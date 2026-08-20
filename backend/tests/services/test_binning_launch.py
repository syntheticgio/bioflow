"""The launch path for MetaBAT2 binning.

Same posture as test_scaffold_launch.py: run the whole way to the enqueue
rather than stopping at "the service raised nothing".

The ambiguity this launch actually guards is *which alignment*, and one check
in particular has no analog elsewhere -- that the BAM is an alignment of the
very assembly being binned. Binning against coverage from some other reference
is not a worse answer, it is a meaningless one, and it fails deep inside the
job (as an empty depth file) rather than at launch unless caught here.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.errors import ValidationError
from app.models import FormatKind, ObjectRole, ObjectStatus, SidecarRole
from app.models.run import RunInput, RunInputRole, RunKind
from app.pipelines.tools import Tool
from app.services import pipeline_service
from tests.services.conftest import _obj

_MB = Tool(name="metabat2", path="/usr/local/bin/metabat2", version="2.18")


def _assembly(*, project_id=None, facts=None):
    obj = _obj(
        name="community.assembly.fasta",
        kind=FormatKind.FASTA,
        role=ObjectRole.REFERENCE,
        project_id=project_id,
    )
    obj.facts = facts or {"assembly_meta_mode": True}
    return obj


def _alignment(*, project_id, derived_from):
    obj = _obj(
        name="reads.bam",
        kind=FormatKind.BAM,
        role=ObjectRole.ALIGNMENT,
        project_id=project_id,
    )
    obj.derived_from = derived_from
    return obj


async def _run(*, contigs, bam, params=None, bai=True):
    objects = {contigs.id: contigs, bam.id: bam}

    async def _get_object(object_id, *, owner):
        return objects.get(object_id)

    async def _sidecar(obj, role):
        if role is SidecarRole.BAI and bai and obj.id == bam.id:
            return _obj(name="reads.bam.bai", kind=FormatKind.BAM)
        return None

    enqueued: dict = {}

    async def _enqueue(job_type, **kwargs):
        enqueued["type"] = job_type
        enqueued.update(kwargs)
        return SimpleNamespace(id="job1")

    created: dict = {}

    # Constructing the real RunInput is the assertion, as in
    # test_scaffold_launch.py: it is a pydantic model with a required `name`,
    # so a launcher that omits one raises here rather than in the running app.
    async def _create_run(**kwargs):
        for item in kwargs["inputs"]:
            assert isinstance(item, RunInput)
            assert item.name
        created.update(kwargs)
        return SimpleNamespace(id="run1", owner="local")

    with (
        patch("app.pipelines.tools.metabat2", return_value=_MB),
        patch(
            "app.services.object_service.get_object",
            AsyncMock(side_effect=_get_object),
        ),
        patch(
            "app.services.pipeline_service._sidecar_of_role",
            AsyncMock(side_effect=_sidecar),
        ),
        patch(
            "app.services.pipeline_service._resolve_readable",
            AsyncMock(return_value=("a" * 64, None)),
        ),
        patch("app.services.run_service.create_run", _create_run),
        patch("app.services.run_service.link_job", AsyncMock()),
        patch("app.queue.queue.enqueue", _enqueue),
    ):
        job = await pipeline_service.launch_binning(
            object_id=contigs.id,
            alignment_ids=[bam.id],
            owner="local",
            params=params,
        )
    return job, enqueued, created


class TestTheHappyPath:
    async def test_the_assembly_and_its_alignment_reach_the_queue(self):
        contigs = _assembly()
        bam = _alignment(project_id=contigs.project_id, derived_from=[contigs.id])

        job, enqueued, created = await _run(contigs=contigs, bam=bam)

        assert job.id == "job1"
        assert enqueued["type"] == "binning"
        payload = enqueued["payload"]
        assert payload["contigs_id"] == str(contigs.id)
        assert payload["bam_object_id"] == str(bam.id)
        assert payload["min_contig"] == 2500

    async def test_the_run_records_both_inputs(self):
        contigs = _assembly()
        bam = _alignment(project_id=contigs.project_id, derived_from=[contigs.id])

        _job, _enqueued, created = await _run(contigs=contigs, bam=bam)

        assert created["kind"] is RunKind.BINNING
        assert created["tool"] == "metabat2"
        roles = {i.role for i in created["inputs"]}
        assert roles == {RunInputRole.ASSEMBLY, RunInputRole.ALIGNMENT}

    async def test_parameters_are_carried_through(self):
        contigs = _assembly()
        bam = _alignment(project_id=contigs.project_id, derived_from=[contigs.id])

        _job, enqueued, _created = await _run(
            contigs=contigs, bam=bam, params={"min_contig": 5000, "threads": 8}
        )

        assert enqueued["payload"]["min_contig"] == 5000
        assert enqueued["resources"].cpu == 8


class TestTheInputsItRefuses:
    async def test_an_alignment_of_something_else_is_refused(self):
        """The check with no analog in the other launchers.

        A BAM against a different reference produces an empty depth file deep
        inside the job, and the resulting error names a file rather than the
        wrong input that caused it.
        """
        contigs = _assembly()
        other = _assembly(project_id=contigs.project_id)
        bam = _alignment(project_id=contigs.project_id, derived_from=[other.id])

        with pytest.raises(ValidationError, match="not an alignment against"):
            await _run(contigs=contigs, bam=bam)

    async def test_an_unindexed_bam_is_refused(self):
        contigs = _assembly()
        bam = _alignment(project_id=contigs.project_id, derived_from=[contigs.id])

        with pytest.raises(ValidationError, match=r"\.bai"):
            await _run(contigs=contigs, bam=bam, bai=False)

    async def test_a_non_fasta_assembly_is_refused(self):
        contigs = _obj(name="calls.vcf", kind=FormatKind.VCF)
        contigs.facts = {}
        bam = _alignment(project_id=contigs.project_id, derived_from=[contigs.id])

        with pytest.raises(ValidationError, match="not a FASTA"):
            await _run(contigs=contigs, bam=bam)

    async def test_an_unready_assembly_is_refused(self):
        contigs = _assembly()
        contigs.status = ObjectStatus.INGESTING
        bam = _alignment(project_id=contigs.project_id, derived_from=[contigs.id])

        with pytest.raises(ValidationError, match="not ready"):
            await _run(contigs=contigs, bam=bam)

    async def test_no_alignment_at_all_is_refused(self):
        contigs = _assembly()
        with pytest.raises(ValidationError, match="needs an alignment"):
            with patch("app.pipelines.tools.metabat2", return_value=_MB):
                await pipeline_service.launch_binning(
                    object_id=contigs.id, alignment_ids=[], owner="local"
                )

    async def test_a_min_contig_below_metabat2s_floor_is_refused(self):
        """Caught at launch rather than after the depth step has run."""
        contigs = _assembly()
        bam = _alignment(project_id=contigs.project_id, derived_from=[contigs.id])

        with pytest.raises(ValidationError, match="1500"):
            await _run(contigs=contigs, bam=bam, params={"min_contig": 500})


class TestTheMetaModeGateIsSoft:
    async def test_an_isolate_assembly_may_still_be_binned(self):
        """Binning a non-`--meta` assembly is unusual rather than wrong -- a
        contaminated isolate is exactly a case someone might want to bin -- so
        the launcher allows it and the card explains it."""
        contigs = _assembly(facts={})
        bam = _alignment(project_id=contigs.project_id, derived_from=[contigs.id])

        job, _enqueued, _created = await _run(contigs=contigs, bam=bam)
        assert job.id == "job1"
