"""The launch path for the GC-vs-coverage bias curve (gc_bias).

Same posture test_consensus_launch.py takes: build SimpleNamespace fakes for
the BAM/reference, patch `object_service.get_object` to a dict lookup and
`queue.enqueue` to a spy, and run the launcher for real rather than stopping
at "the service raised nothing".

Three preconditions, each refused by name (V2, docs/superpowers/specs/
2026-08-20-gc-coverage-visualizations-design.md): the BAM's alignment target
must resolve, that target must have run gc_tracks, and the BAM must have a
*windowed* (not region-mode) coverage run.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from beanie import PydanticObjectId

from app.errors import ConflictError, ValidationError
from app.services import pipeline_service


def _obj(
    *,
    name,
    kind=None,
    role=None,
    derived_from=None,
    facts=None,
    project_id=None,
):
    from app.models import FormatKind, ObjectStatus

    return SimpleNamespace(
        id=PydanticObjectId(),
        name=name,
        format=SimpleNamespace(kind=kind or FormatKind.BAM),
        role=role,
        status=ObjectStatus.READY,
        derived_from=derived_from or [],
        facts=facts or {},
        project_id=project_id or PydanticObjectId(),
        owner="local",
        blob_sha256="a" * 64,
    )


def _reference(*, facts=None, project_id=None):
    from app.models import FormatKind, ObjectRole

    return _obj(
        name="ref.fasta",
        kind=FormatKind.FASTA,
        role=ObjectRole.REFERENCE,
        project_id=project_id,
        facts=facts,
    )


def _bam(*, reference=None, facts=None, project_id=None):
    from app.models import FormatKind, ObjectRole

    return _obj(
        name="aln.bam",
        kind=FormatKind.BAM,
        role=ObjectRole.ALIGNMENT,
        derived_from=[reference.id] if reference is not None else [],
        project_id=project_id or (reference.project_id if reference else None),
        facts=facts,
    )


async def _launch(*, bam, reference=None, objects=None, report=None, tmp_path):
    """Run launch_gc_bias with object_service and queue patched, and (when a
    report dict is given) a real coverage report JSON written to tmp_path so
    the launcher's file read resolves like it would in production."""
    objects = objects or {}
    objects.setdefault(bam.id, bam)
    if reference is not None:
        objects.setdefault(reference.id, reference)

    async def _get_object(object_id, *, owner):
        assert owner == "local"
        return objects[object_id]

    enqueued = {}

    async def _enqueue(job_type, **kwargs):
        enqueued["type"] = job_type
        enqueued.update(kwargs)
        return SimpleNamespace(id="job1")

    if report is not None:
        report_dir = tmp_path / "coverage" / str(bam.id)
        report_dir.mkdir(parents=True, exist_ok=True)
        report_name = bam.facts.get("coverage_report", "coverage.json")
        (report_dir / report_name).write_text(json.dumps(report))

    with (
        patch(
            "app.services.object_service.get_object",
            AsyncMock(side_effect=_get_object),
        ),
        patch.object(pipeline_service.settings, "bioinfo_home", tmp_path),
        patch("app.queue.queue.enqueue", _enqueue),
    ):
        job = await pipeline_service.launch_gc_bias(bam_id=bam.id, owner="local")
    return job, enqueued


class TestLaunchGcBiasPreconditions:
    async def test_refuses_when_alignment_target_unresolved(self, tmp_path):
        bam = _bam(facts={})  # no derived_from -> no target

        with pytest.raises(ValidationError, match="no recorded alignment target"):
            await _launch(bam=bam, tmp_path=tmp_path)

    async def test_refuses_when_reference_has_no_gc_tracks(self, tmp_path):
        reference = _reference(facts={})  # no gc_tracks fact
        bam = _bam(reference=reference, facts={})

        with pytest.raises(ValidationError, match="[Gg][Cc] [Tt]racks"):
            await _launch(bam=bam, reference=reference, tmp_path=tmp_path)

    async def test_refuses_when_bam_has_no_coverage(self, tmp_path):
        reference = _reference(
            facts={"gc_tracks": {"window_count": 500, "contigs": [{"name": "chr1", "gc": [50.0]}]}}
        )
        bam = _bam(reference=reference, facts={})

        with pytest.raises(ValidationError, match="coverage"):
            await _launch(bam=bam, reference=reference, tmp_path=tmp_path)

    async def test_refuses_when_coverage_is_region_mode(self, tmp_path):
        """A region-mode coverage run is not on the shared window grid; the
        join cannot use it, and this must read exactly like 'no coverage
        run' rather than crash on a shape mismatch."""
        reference = _reference(
            facts={"gc_tracks": {"window_count": 500, "contigs": [{"name": "chr1", "gc": [50.0]}]}}
        )
        bam = _bam(
            reference=reference,
            facts={"coverage_status": "ok", "coverage_mode": "regions"},
        )

        with pytest.raises(ValidationError, match="windowed"):
            await _launch(bam=bam, reference=reference, tmp_path=tmp_path)


class TestLaunchGcBiasReachesTheQueue:
    async def test_success_reaches_the_queue_with_gc_contigs_and_depth_regions(
        self, tmp_path
    ):
        gc_contigs = [
            {
                "name": "chr1", "length": 1000, "window_bases": 500,
                "gc": [50.0, 55.0], "skew": [0.1, -0.1],
            },
        ]
        reference = _reference(
            facts={"gc_tracks": {"window_count": 500, "contigs": gc_contigs}}
        )
        depth_regions = {"chr1": [{"start": 0, "end": 100, "depth": 12.5}]}
        bam = _bam(
            reference=reference,
            facts={
                "coverage_status": "ok",
                "coverage_mode": "windows",
                "coverage_report": "coverage.json",
            },
        )

        job, enqueued = await _launch(
            bam=bam,
            reference=reference,
            report={"regions": depth_regions},
            tmp_path=tmp_path,
        )

        assert job.id == "job1"
        assert enqueued["type"] == "gc_bias"
        assert enqueued["payload"]["bam_id"] == str(bam.id)
        assert enqueued["payload"]["gc_contigs"] == [
            {"name": "chr1", "length": 1000, "window_bases": 500, "gc": [50.0, 55.0]},
        ]
        assert enqueued["payload"]["depth_regions"] == depth_regions

    async def test_payload_contigs_do_not_carry_skew(self, tmp_path):
        """Minor #3 finding: skew is never read by join_windows/bias_curve
        and roughly a third of a full reference's contig payload size."""
        gc_contigs = [
            {
                "name": "chr1", "length": 1000, "window_bases": 500,
                "gc": [50.0, 55.0], "skew": [0.1, -0.1],
            },
        ]
        reference = _reference(
            facts={"gc_tracks": {"window_count": 500, "contigs": gc_contigs}}
        )
        bam = _bam(
            reference=reference,
            facts={
                "coverage_status": "ok",
                "coverage_mode": "windows",
                "coverage_report": "coverage.json",
            },
        )

        _, enqueued = await _launch(
            bam=bam,
            reference=reference,
            report={"regions": {}},
            tmp_path=tmp_path,
        )

        for contig in enqueued["payload"]["gc_contigs"]:
            assert "skew" not in contig

    async def test_payload_carries_gc_tracks_partial_flag(self, tmp_path):
        """Important #1 finding: the truncation flag gc_tracks.py sets when
        it caps at MAX_STORED_CONTIGS must ride along in the gc_bias
        payload so the handler and eventually the chart can surface it."""
        gc_contigs = [{"name": "chr1", "length": 1000, "window_bases": 500, "gc": [50.0]}]
        reference = _reference(
            facts={
                "gc_tracks": {
                    "window_count": 500,
                    "contigs": gc_contigs,
                    "gc_tracks_partial": True,
                }
            }
        )
        bam = _bam(
            reference=reference,
            facts={
                "coverage_status": "ok",
                "coverage_mode": "windows",
                "coverage_report": "coverage.json",
            },
        )

        _, enqueued = await _launch(
            bam=bam,
            reference=reference,
            report={"regions": {}},
            tmp_path=tmp_path,
        )

        assert enqueued["payload"]["gc_tracks_partial"] is True

    async def test_payload_gc_tracks_partial_defaults_false(self, tmp_path):
        gc_contigs = [{"name": "chr1", "length": 1000, "window_bases": 500, "gc": [50.0]}]
        reference = _reference(
            facts={"gc_tracks": {"window_count": 500, "contigs": gc_contigs}}
        )
        bam = _bam(
            reference=reference,
            facts={
                "coverage_status": "ok",
                "coverage_mode": "windows",
                "coverage_report": "coverage.json",
            },
        )

        _, enqueued = await _launch(
            bam=bam,
            reference=reference,
            report={"regions": {}},
            tmp_path=tmp_path,
        )

        assert enqueued["payload"]["gc_tracks_partial"] is False

    async def test_deduplicated_launch_raises_conflict(self, tmp_path):
        gc_contigs = [
            {"name": "chr1", "length": 1000, "window_bases": 500, "gc": [50.0]},
        ]
        reference = _reference(
            facts={"gc_tracks": {"window_count": 500, "contigs": gc_contigs}}
        )
        bam = _bam(
            reference=reference,
            facts={
                "coverage_status": "ok",
                "coverage_mode": "windows",
                "coverage_report": "coverage.json",
            },
        )
        objects = {bam.id: bam, reference.id: reference}

        async def _get_object(object_id, *, owner):
            return objects[object_id]

        report_dir = tmp_path / "coverage" / str(bam.id)
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "coverage.json").write_text(json.dumps({"regions": {}}))

        with (
            patch(
                "app.services.object_service.get_object",
                AsyncMock(side_effect=_get_object),
            ),
            patch.object(pipeline_service.settings, "bioinfo_home", tmp_path),
            patch("app.queue.queue.enqueue", AsyncMock(return_value=None)),
        ):
            with pytest.raises(ConflictError):
                await pipeline_service.launch_gc_bias(bam_id=bam.id, owner="local")
