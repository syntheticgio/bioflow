"""from_parameter_set reaches PipelineRun through the align/assemble launchers.

Task 8 (frontend, merged) sends `from_parameter_set` on launch requests when a
saved parameter set configured the run. `run_service.create_run` already
accepts and stores it (Task 5), but `pipeline_service.launch_alignment` and
`launch_assembly` never accepted or forwarded it -- the field was silently
dropped between the route and the run record. These tests exercise the two
launch functions directly (rather than through the HTTP route, which no
existing test in this repo does for /align or /assemble) and assert the value
reaches the `run_service.create_run` call, following the same
mock-create_run-and-inspect-kwargs pattern as
tests/pipelines/test_align_extra_reads.py and
tests/services/test_assembly_launch.py.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from beanie import PydanticObjectId

from app.models import FormatKind, ObjectStatus
from app.models.run import AppliedParameterSet
from app.services import memory_estimate, pipeline_service


def _memory_estimate(mb: int) -> memory_estimate.MemoryEstimate:
    return memory_estimate.MemoryEstimate(
        mb=mb,
        source=memory_estimate.EstimateSource.HEURISTIC,
        detail="from published tool coefficients",
    )


def _fastq_object(object_id, name, *, project_id):
    return SimpleNamespace(
        id=object_id,
        name=name,
        format=SimpleNamespace(kind=FormatKind.FASTQ),
        role=None,
        facts={},
        metadata={},
        status=ObjectStatus.READY,
        project_id=project_id,
        owner="local",
        size=1_000_000,
    )


class TestLaunchAlignmentCarriesFromParameterSet:
    async def test_from_parameter_set_reaches_create_run(self):
        project_id = PydanticObjectId()
        primary = _fastq_object(PydanticObjectId(), "sample_R1.fastq", project_id=project_id)
        reference = SimpleNamespace(
            id=PydanticObjectId(),
            name="ref.fasta",
            format=SimpleNamespace(kind=FormatKind.FASTA),
            role=None,
            facts={},
            status=ObjectStatus.READY,
            project_id=project_id,
            owner="local",
            size=3_000_000_000,
        )
        applied = AppliedParameterSet(
            set_id=PydanticObjectId(),
            name="Nanopore fast",
            revision=1,
            edited_after_apply=False,
        )

        create_run = AsyncMock(return_value=SimpleNamespace(id="run1", owner="local"))

        async def _enqueue(job_type, **kwargs):
            return SimpleNamespace(id=PydanticObjectId())

        async def _get_object(object_id, owner):
            for obj in (primary, reference):
                if obj.id == object_id:
                    return obj
            raise AssertionError(f"unexpected object id {object_id}")

        with (
            patch(
                "app.services.object_service.get_object",
                AsyncMock(side_effect=_get_object),
            ),
            patch(
                "app.services.pipeline_service.reference_index_status",
                AsyncMock(return_value={"minimap2": True, "fai": True}),
            ),
            patch(
                "app.services.memory_estimate.resolve",
                AsyncMock(return_value=_memory_estimate(1024)),
            ),
            patch(
                "app.services.pipeline_service._resolve_readable",
                AsyncMock(return_value=(None, None)),
            ),
            patch(
                "app.services.pipeline_service.sidecar_payload",
                AsyncMock(return_value={}),
            ),
            patch("app.services.run_service.create_run", create_run),
            patch("app.services.run_service.link_job", AsyncMock()),
            patch("app.queue.queue.enqueue", _enqueue),
        ):
            await pipeline_service.launch_alignment(
                object_id=primary.id,
                reference_id=reference.id,
                owner="local",
                paired=False,
                from_parameter_set=applied,
            )

        assert create_run.call_args.kwargs["from_parameter_set"] == applied

    async def test_omitted_from_parameter_set_stores_none(self):
        """The common case (no saved set involved) must reach create_run as
        None rather than being omitted -- create_run's own default already
        covers a caller that never passes the kwarg, but launch_alignment
        always passes it explicitly, so the value here should be None."""
        project_id = PydanticObjectId()
        primary = _fastq_object(PydanticObjectId(), "sample_R1.fastq", project_id=project_id)
        reference = SimpleNamespace(
            id=PydanticObjectId(),
            name="ref.fasta",
            format=SimpleNamespace(kind=FormatKind.FASTA),
            role=None,
            facts={},
            status=ObjectStatus.READY,
            project_id=project_id,
            owner="local",
            size=3_000_000_000,
        )

        create_run = AsyncMock(return_value=SimpleNamespace(id="run1", owner="local"))

        async def _enqueue(job_type, **kwargs):
            return SimpleNamespace(id=PydanticObjectId())

        async def _get_object(object_id, owner):
            for obj in (primary, reference):
                if obj.id == object_id:
                    return obj
            raise AssertionError(f"unexpected object id {object_id}")

        with (
            patch(
                "app.services.object_service.get_object",
                AsyncMock(side_effect=_get_object),
            ),
            patch(
                "app.services.pipeline_service.reference_index_status",
                AsyncMock(return_value={"minimap2": True, "fai": True}),
            ),
            patch(
                "app.services.memory_estimate.resolve",
                AsyncMock(return_value=_memory_estimate(1024)),
            ),
            patch(
                "app.services.pipeline_service._resolve_readable",
                AsyncMock(return_value=(None, None)),
            ),
            patch(
                "app.services.pipeline_service.sidecar_payload",
                AsyncMock(return_value={}),
            ),
            patch("app.services.run_service.create_run", create_run),
            patch("app.services.run_service.link_job", AsyncMock()),
            patch("app.queue.queue.enqueue", _enqueue),
        ):
            await pipeline_service.launch_alignment(
                object_id=primary.id,
                reference_id=reference.id,
                owner="local",
                paired=False,
            )

        assert create_run.call_args.kwargs["from_parameter_set"] is None


def _assembly_reads_fixture():
    return SimpleNamespace(
        id=PydanticObjectId(),
        name="SRR1.fastq",
        format=SimpleNamespace(kind=FormatKind.FASTQ),
        role=None,
        metadata={"organism": "Saccharomyces cerevisiae"},
        facts={"qc_read_chemistry": "hifi"},
        status=ObjectStatus.READY,
        project_id=PydanticObjectId(),
        owner="local",
        blob_sha256="a" * 64,
        size=1_000_000,
    )


class TestLaunchAssemblyCarriesFromParameterSet:
    """Mirrors TestAssemblyRefusal.test_override_skips_the_refusal's mocking
    seams in test_launch_resource_refusal.py -- object_service.get_object and
    _resolve_readable are the two that matter for reaching create_run."""

    async def test_from_parameter_set_reaches_create_run(self):
        reads = _assembly_reads_fixture()
        applied = AppliedParameterSet(
            set_id=PydanticObjectId(),
            name="Flye default",
            revision=2,
            edited_after_apply=True,
        )

        create_run = AsyncMock(return_value=SimpleNamespace(id="run1", owner="local"))

        async def _enqueue(job_type, **kwargs):
            return SimpleNamespace(id=PydanticObjectId())

        with (
            patch(
                "app.services.object_service.get_object",
                AsyncMock(return_value=reads),
            ),
            patch(
                "app.services.memory_estimate.resolve",
                AsyncMock(return_value=_memory_estimate(1024)),
            ),
            patch(
                "app.services.pipeline_service._resolve_readable",
                AsyncMock(return_value=("a" * 64, None)),
            ),
            patch("app.services.run_service.create_run", create_run),
            patch("app.services.run_service.link_job", AsyncMock()),
            patch("app.queue.queue.enqueue", _enqueue),
        ):
            await pipeline_service.launch_assembly(
                object_id=reads.id,
                owner="local",
                params={
                    "assembler": "flye",
                    "mode": "pacbio-hifi",
                    "threads": 8,
                    "iterations": 1,
                    "genome_size": 12_000_000,
                    "genome_size_source": "user",
                },
                from_parameter_set=applied,
            )

        assert create_run.call_args.kwargs["from_parameter_set"] == applied
