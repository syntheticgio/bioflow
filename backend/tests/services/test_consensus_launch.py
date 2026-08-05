"""The launch path for iVar consensus calling.

Same posture test_completeness_launch.py takes: run the whole way to the
enqueue rather than stopping at "the service raised nothing".
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from beanie import PydanticObjectId

from app.errors import ValidationError
from app.models import FormatKind, ObjectRole, ObjectStatus
from app.pipelines.tools import Tool
from app.services import pipeline_service

_IVAR_TOOL = Tool(name="ivar", path="/usr/bin/ivar", version="1.4.4")


def _obj(
    *,
    name,
    kind,
    role=None,
    status=ObjectStatus.READY,
    derived_from=None,
    facts=None,
    project_id=None,
):
    return SimpleNamespace(
        id=PydanticObjectId(),
        name=name,
        format=SimpleNamespace(kind=kind),
        role=role,
        status=status,
        derived_from=derived_from or [],
        facts=facts or {},
        project_id=project_id or PydanticObjectId(),
        owner="local",
        blob_sha256="a" * 64,
    )


def _reference(*, project_id=None, names=("MN908947.3",)):
    return _obj(
        name="ref.fasta",
        kind=FormatKind.FASTA,
        role=ObjectRole.REFERENCE,
        project_id=project_id,
        facts={"reference_names": list(names)},
    )


def _bam(*, reference, project_id=None):
    return _obj(
        name="aln.bam",
        kind=FormatKind.BAM,
        role=ObjectRole.ALIGNMENT,
        derived_from=[reference.id],
        project_id=project_id or reference.project_id,
    )


def _primer_bed(*, names=("MN908947.3",), column_counts=(6,), project_id=None):
    return _obj(
        name="primers.bed",
        kind=FormatKind.BED,
        project_id=project_id,
        facts={"reference_names": list(names), "column_counts": list(column_counts)},
    )


class TestLaunchConsensusReachesTheQueue:
    async def _run(self, *, bam, reference, primer_bed=None, objects=None):
        objects = objects or {}
        objects.setdefault(bam.id, bam)
        objects.setdefault(reference.id, reference)
        if primer_bed is not None:
            objects.setdefault(primer_bed.id, primer_bed)

        async def _get_object(object_id, *, owner):
            assert owner == "local"
            return objects[object_id]

        enqueued = {}

        async def _enqueue(job_type, **kwargs):
            enqueued["type"] = job_type
            enqueued.update(kwargs)
            return SimpleNamespace(id="job1")

        with (
            patch("app.pipelines.tools.ivar", return_value=_IVAR_TOOL),
            patch(
                "app.services.object_service.get_object",
                AsyncMock(side_effect=_get_object),
            ),
            patch(
                "app.services.pipeline_service._resolve_readable",
                AsyncMock(return_value=("a" * 64, None)),
            ),
            patch("app.queue.queue.enqueue", _enqueue),
        ):
            job = await pipeline_service.launch_consensus(
                bam_object_id=bam.id,
                primer_bed_object_id=primer_bed.id if primer_bed else None,
                owner="local",
            )
        return job, enqueued

    async def test_matching_bam_and_reference_reach_the_queue(self):
        reference = _reference()
        bam = _bam(reference=reference)

        job, enqueued = await self._run(bam=bam, reference=reference)

        assert job.id == "job1"
        assert enqueued["type"] == "consensus_from_alignment"
        assert enqueued["payload"]["reference_object_id"] == str(reference.id)
        assert enqueued["payload"].get("primer_bed_sha256") is None

    async def test_primer_bed_is_threaded_through_when_supplied(self):
        reference = _reference()
        bam = _bam(reference=reference)
        primer_bed = _primer_bed(project_id=reference.project_id)

        job, enqueued = await self._run(bam=bam, reference=reference, primer_bed=primer_bed)

        assert enqueued["payload"]["primer_bed_sha256"] == "a" * 64

    async def test_disjoint_primer_bed_is_refused_before_enqueue(self):
        reference = _reference(names=("MN908947.3",))
        bam = _bam(reference=reference)
        primer_bed = _primer_bed(names=("NC_045512.2",), project_id=reference.project_id)
        enqueue = AsyncMock()

        with (
            patch("app.pipelines.tools.ivar", return_value=_IVAR_TOOL),
            patch(
                "app.services.object_service.get_object",
                AsyncMock(
                    side_effect=lambda oid, *, owner: {
                        bam.id: bam,
                        reference.id: reference,
                        primer_bed.id: primer_bed,
                    }[oid]
                ),
            ),
            patch(
                "app.services.pipeline_service._resolve_readable",
                AsyncMock(return_value=("a" * 64, None)),
            ),
            patch("app.queue.queue.enqueue", enqueue),
        ):
            with pytest.raises(ValidationError, match="no contigs in common"):
                await pipeline_service.launch_consensus(
                    bam_object_id=bam.id,
                    primer_bed_object_id=primer_bed.id,
                    owner="local",
                )
        enqueue.assert_not_awaited()

    async def test_reference_is_always_resolved_from_bam_provenance(self):
        """launch_consensus takes no explicit reference argument -- the
        foundation's provenance rule (#21) is the only way a reference is
        ever chosen, so there is no parallel path that could disagree with
        it. A BAM aligned to other_reference always resolves to
        other_reference, never to some other object the caller might have
        had in mind."""
        other_reference = _reference(names=("NC_045512.2",))
        bam = _bam(reference=other_reference)
        enqueue = AsyncMock()

        async def _enqueue(job_type, **kwargs):
            enqueue_calls.append(kwargs)
            return SimpleNamespace(id="job1")

        enqueue_calls = []

        with (
            patch("app.pipelines.tools.ivar", return_value=_IVAR_TOOL),
            patch(
                "app.services.object_service.get_object",
                AsyncMock(
                    side_effect=lambda oid, *, owner: {
                        bam.id: bam,
                        other_reference.id: other_reference,
                    }[oid]
                ),
            ),
            patch(
                "app.services.pipeline_service._resolve_readable",
                AsyncMock(return_value=("a" * 64, None)),
            ),
            patch("app.queue.queue.enqueue", _enqueue),
        ):
            job = await pipeline_service.launch_consensus(
                bam_object_id=bam.id, owner="local"
            )

        assert job.id == "job1"
        assert enqueue_calls[0]["payload"]["reference_object_id"] == str(
            other_reference.id
        )

    async def test_ivar_not_installed_is_refused_before_anything_else(self):
        missing = Tool(name="ivar", path=None, version=None, error="not found")
        reference = _reference()
        bam = _bam(reference=reference)
        get_object = AsyncMock(return_value=bam)

        with patch("app.pipelines.tools.ivar", return_value=missing):
            with pytest.raises(Exception):  # PermanentError from tools.require
                await pipeline_service.launch_consensus(
                    bam_object_id=bam.id, owner="local"
                )
        get_object.assert_not_awaited()

    async def test_not_a_bam_is_refused(self):
        obj = _obj(name="notes.txt", kind=FormatKind.TEXT)

        with (
            patch("app.pipelines.tools.ivar", return_value=_IVAR_TOOL),
            patch(
                "app.services.object_service.get_object",
                AsyncMock(return_value=obj),
            ),
        ):
            with pytest.raises(ValidationError, match="not an alignment"):
                await pipeline_service.launch_consensus(
                    bam_object_id=obj.id, owner="local"
                )
