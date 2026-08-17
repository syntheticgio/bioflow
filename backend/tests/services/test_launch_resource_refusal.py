"""The enqueue-time refusal, its payload, and the override that skips it."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from beanie import PydanticObjectId

from app.errors import ValidationError
from app.models import FormatKind, ObjectStatus
from app.pipelines import resource_estimator
from app.services import memory_estimate, pipeline_service


def _align_fixture():
    """Reads and reference shaped like `launch_alignment` expects, following
    the SimpleNamespace pattern from test_assembly_launch.py's
    TestLaunchReachesTheQueue._reads_object. Only what the launch path reads
    before reaching the BLOCK check needs to be real-shaped.
    """
    project_id = PydanticObjectId()
    reads = SimpleNamespace(
        id=PydanticObjectId(),
        name="SRR1.fastq",
        format=SimpleNamespace(kind=FormatKind.FASTQ),
        role=None,
        facts={},
        metadata={},
        status=ObjectStatus.READY,
        project_id=project_id,
        owner="local",
        size=1_000_000,
    )
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
    return reads, reference


def _memory_estimate(mb: int) -> memory_estimate.MemoryEstimate:
    return memory_estimate.MemoryEstimate(
        mb=mb,
        source=memory_estimate.EstimateSource.HEURISTIC,
        detail="from published tool coefficients",
    )


class TestAlignmentRefusal:
    async def test_refusal_details_name_the_estimate_source(self):
        """The card's source line is an acceptance criterion, so the payload
        that feeds it is asserted here rather than left to the UI."""
        reads, reference = _align_fixture()

        with (
            patch(
                "app.services.object_service.get_object",
                AsyncMock(side_effect=[reads, reference]),
            ),
            patch(
                "app.services.pipeline_service.reference_index_status",
                AsyncMock(return_value={"minimap2": True, "fai": True}),
            ),
            patch(
                "app.pipelines.resource_estimator.classify",
                lambda **kwargs: resource_estimator.Band.BLOCK,
            ),
            patch(
                "app.services.memory_estimate.resolve",
                AsyncMock(return_value=_memory_estimate(999_999)),
            ),
        ):
            with pytest.raises(ValidationError) as exc:
                await pipeline_service.launch_alignment(
                    object_id=reads.id,
                    reference_id=reference.id,
                    owner="local",
                    paired=False,
                )

        details = exc.value.details
        assert details["estimate_source"] in {"measured", "heuristic"}
        assert isinstance(details["detail"], str) and details["detail"]
        assert details["estimate_mb"] > details["budget_mb"]
        assert "replan" in details
        assert details["replan"]["kind"] in {"proposal", "infeasible", "no_knobs"}

    async def test_override_skips_the_refusal(self):
        """The same call that raised above must succeed with the override set."""
        reads, reference = _align_fixture()

        enqueued = {}

        async def _enqueue(job_type, **kwargs):
            enqueued["type"] = job_type
            enqueued.update(kwargs)
            return SimpleNamespace(id=PydanticObjectId())

        with (
            patch(
                "app.services.object_service.get_object",
                AsyncMock(side_effect=[reads, reference]),
            ),
            patch(
                "app.services.pipeline_service.reference_index_status",
                AsyncMock(return_value={"minimap2": True, "fai": True}),
            ),
            patch(
                "app.pipelines.resource_estimator.classify",
                lambda **kwargs: resource_estimator.Band.BLOCK,
            ),
            patch(
                "app.services.memory_estimate.resolve",
                AsyncMock(return_value=_memory_estimate(999_999)),
            ),
            patch(
                "app.services.pipeline_service._resolve_readable",
                AsyncMock(return_value=(None, None)),
            ),
            patch(
                "app.services.pipeline_service.sidecar_payload",
                AsyncMock(return_value={}),
            ),
            patch(
                "app.services.run_service.create_run",
                AsyncMock(return_value=SimpleNamespace(id="run1", owner="local")),
            ),
            patch("app.services.run_service.link_job", AsyncMock()),
            patch("app.queue.queue.enqueue", _enqueue),
        ):
            job = await pipeline_service.launch_alignment(
                object_id=reads.id,
                reference_id=reference.id,
                owner="local",
                paired=False,
                resource_override=True,
            )

        assert job is not None
        assert enqueued["resource_override"] is True


def _assembly_reads_fixture():
    """A HiFi FASTQ shaped like `launch_assembly` expects, following
    test_assembly_launch.py's TestLaunchReachesTheQueue._reads_object.
    """
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


class TestAssemblyRefusal:
    """Mirrors TestAlignmentRefusal, adapted to launch_assembly's seams:
    object_service.get_object, resource_estimator.classify, and
    memory_estimate.resolve are the same three; launch_assembly has no
    reference to fetch and no index status to check.
    """

    async def test_refusal_details_name_the_estimate_source(self):
        reads = _assembly_reads_fixture()

        with (
            patch(
                "app.services.object_service.get_object",
                AsyncMock(return_value=reads),
            ),
            patch(
                "app.pipelines.resource_estimator.classify",
                lambda **kwargs: resource_estimator.Band.BLOCK,
            ),
            patch(
                "app.services.memory_estimate.resolve",
                AsyncMock(return_value=_memory_estimate(999_999)),
            ),
            # A generous admission budget so the #478 declared-vs-budget
            # refusal (which runs before this heuristic BLOCK check) does not
            # preempt it -- this test is pinning the heuristic refusal's own
            # message shape, not the declared-budget one.
            patch(
                "app.services.pipeline_service.current_admission_budget_mb",
                AsyncMock(return_value=10_000_000),
            ),
        ):
            with pytest.raises(ValidationError) as exc:
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
                )

        details = exc.value.details
        assert details["estimate_source"] in {"measured", "heuristic"}
        assert isinstance(details["detail"], str) and details["detail"]
        assert details["estimate_mb"] > details["budget_mb"]
        assert "replan" in details
        assert details["replan"]["kind"] in {"proposal", "infeasible", "no_knobs"}

    async def test_override_skips_the_refusal(self):
        reads = _assembly_reads_fixture()

        enqueued = {}

        async def _enqueue(job_type, **kwargs):
            enqueued["type"] = job_type
            enqueued.update(kwargs)
            return SimpleNamespace(id=PydanticObjectId())

        with (
            patch(
                "app.services.object_service.get_object",
                AsyncMock(return_value=reads),
            ),
            patch(
                "app.pipelines.resource_estimator.classify",
                lambda **kwargs: resource_estimator.Band.BLOCK,
            ),
            patch(
                "app.services.memory_estimate.resolve",
                AsyncMock(return_value=_memory_estimate(999_999)),
            ),
            patch(
                "app.services.pipeline_service._resolve_readable",
                AsyncMock(return_value=("a" * 64, None)),
            ),
            patch(
                "app.services.run_service.create_run",
                AsyncMock(return_value=SimpleNamespace(id="run1", owner="local")),
            ),
            patch("app.services.run_service.link_job", AsyncMock()),
            patch("app.queue.queue.enqueue", _enqueue),
        ):
            job = await pipeline_service.launch_assembly(
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
                resource_override=True,
            )

        assert job is not None
        assert enqueued["resource_override"] is True


class TestIndexBuildDeclarationIsCheckedBeforeEnqueue:
    """Regression for the ordering bug found in final review (#478 follow-up).

    `_enqueue_build_index` declares MORE memory than the align job for the
    same reference (`declared_align_mem_mb`'s own docstring: a STAR human
    index needs about 30 GB), and `launch_alignment` used to enqueue that
    job before ever computing the align-side declared-vs-budget check --
    so an over-budget index-build declaration reached the queue unchecked
    and could wait forever, exactly the symptom #478 exists to eliminate.
    """

    async def test_over_budget_index_build_refuses_before_enqueueing_anything(self):
        reads, reference = _align_fixture()

        enqueue_calls = []

        async def _enqueue(job_type, **kwargs):
            enqueue_calls.append(job_type)
            return SimpleNamespace(id=PydanticObjectId())

        # declared_align_mem_mb is called twice in the real flow: once for
        # the index build (building_index=True, huge) and once for the align
        # job itself (building_index=False, modest). The huge one must be
        # checked, and checked first.
        async def _declared_align_mem_mb(*, building_index, **kwargs):
            return 999_999 if building_index else 2048

        with (
            patch(
                "app.services.object_service.get_object",
                AsyncMock(side_effect=[reads, reference]),
            ),
            patch(
                "app.services.pipeline_service.reference_index_status",
                # No index and no .fai -- an index build is required.
                AsyncMock(return_value={"minimap2": False, "fai": False}),
            ),
            patch(
                "app.pipelines.resource_estimator.classify",
                lambda **kwargs: resource_estimator.Band.OK,
            ),
            patch(
                "app.services.memory_estimate.resolve",
                AsyncMock(return_value=_memory_estimate(2048)),
            ),
            patch(
                "app.services.pipeline_service.declared_align_mem_mb",
                _declared_align_mem_mb,
            ),
            patch(
                "app.services.pipeline_service.current_admission_budget_mb",
                # Above the modest align declaration (2048) but below the
                # index build's (999_999) -- only the index-build check
                # should fire.
                AsyncMock(return_value=5600),
            ),
            patch(
                "app.services.pipeline_service._resolve_readable",
                AsyncMock(return_value=(None, None)),
            ),
            patch(
                "app.services.run_service.create_run",
                AsyncMock(return_value=SimpleNamespace(id="run1", owner="local")),
            ),
            patch("app.services.run_service.link_job", AsyncMock()),
            patch("app.queue.queue.enqueue", _enqueue),
        ):
            with pytest.raises(ValidationError) as exc:
                await pipeline_service.launch_alignment(
                    object_id=reads.id,
                    reference_id=reference.id,
                    owner="local",
                    paired=False,
                )

        assert "999,999" in str(exc.value)
        # Neither the build_index job nor the align_reads job was ever
        # enqueued -- the whole launch is all-or-nothing.
        assert enqueue_calls == []

    async def test_override_allows_an_over_budget_index_build(self):
        """The same 'launch anyway' flag that governs the align check must
        also govern the index-build check -- it is one user decision for the
        whole launch, not per-job."""
        reads, reference = _align_fixture()

        enqueue_calls = []

        async def _enqueue(job_type, **kwargs):
            enqueue_calls.append(job_type)
            return SimpleNamespace(id=PydanticObjectId())

        async def _declared_align_mem_mb(*, building_index, **kwargs):
            return 999_999 if building_index else 2048

        with (
            patch(
                "app.services.object_service.get_object",
                AsyncMock(side_effect=[reads, reference]),
            ),
            patch(
                "app.services.pipeline_service.reference_index_status",
                AsyncMock(return_value={"minimap2": False, "fai": False}),
            ),
            patch(
                "app.pipelines.resource_estimator.classify",
                lambda **kwargs: resource_estimator.Band.OK,
            ),
            patch(
                "app.services.memory_estimate.resolve",
                AsyncMock(return_value=_memory_estimate(2048)),
            ),
            patch(
                "app.services.pipeline_service.declared_align_mem_mb",
                _declared_align_mem_mb,
            ),
            patch(
                "app.services.pipeline_service._resolve_readable",
                AsyncMock(return_value=(None, None)),
            ),
            patch(
                "app.services.pipeline_service.sidecar_payload",
                AsyncMock(return_value={}),
            ),
            patch(
                "app.services.run_service.create_run",
                AsyncMock(return_value=SimpleNamespace(id="run1", owner="local")),
            ),
            patch("app.services.run_service.link_job", AsyncMock()),
            patch("app.queue.queue.enqueue", _enqueue),
        ):
            job = await pipeline_service.launch_alignment(
                object_id=reads.id,
                reference_id=reference.id,
                owner="local",
                paired=False,
                resource_override=True,
            )

        assert job is not None
        assert enqueue_calls == ["build_index", "align_reads"]

    async def test_admission_budget_resolved_at_most_once(self):
        """`current_admission_budget_mb()` must be shared between the
        index-build and align checks, not resolved twice."""
        reads, reference = _align_fixture()

        async def _enqueue(job_type, **kwargs):
            return SimpleNamespace(id=PydanticObjectId())

        budget_calls = []

        async def _budget():
            budget_calls.append(1)
            return 10_000_000

        with (
            patch(
                "app.services.object_service.get_object",
                AsyncMock(side_effect=[reads, reference]),
            ),
            patch(
                "app.services.pipeline_service.reference_index_status",
                AsyncMock(return_value={"minimap2": False, "fai": False}),
            ),
            patch(
                "app.pipelines.resource_estimator.classify",
                lambda **kwargs: resource_estimator.Band.OK,
            ),
            patch(
                "app.services.memory_estimate.resolve",
                AsyncMock(return_value=_memory_estimate(2048)),
            ),
            patch(
                "app.services.pipeline_service._resolve_readable",
                AsyncMock(return_value=(None, None)),
            ),
            patch(
                "app.services.pipeline_service.sidecar_payload",
                AsyncMock(return_value={}),
            ),
            patch(
                "app.services.run_service.create_run",
                AsyncMock(return_value=SimpleNamespace(id="run1", owner="local")),
            ),
            patch("app.services.run_service.link_job", AsyncMock()),
            patch("app.queue.queue.enqueue", _enqueue),
            patch(
                "app.services.pipeline_service.current_admission_budget_mb",
                _budget,
            ),
        ):
            job = await pipeline_service.launch_alignment(
                object_id=reads.id,
                reference_id=reference.id,
                owner="local",
                paired=False,
            )

        assert job is not None
        assert len(budget_calls) == 1

    async def test_no_index_build_needed_skips_the_index_check(self):
        """An alignment against an already-indexed reference must not be
        refused based on a hypothetical index-build cost it will never pay."""
        reads, reference = _align_fixture()

        enqueue_calls = []

        async def _enqueue(job_type, **kwargs):
            enqueue_calls.append(job_type)
            return SimpleNamespace(id=PydanticObjectId())

        with (
            patch(
                "app.services.object_service.get_object",
                AsyncMock(side_effect=[reads, reference]),
            ),
            patch(
                "app.services.pipeline_service.reference_index_status",
                # Already indexed -- no build_index job should be enqueued
                # or checked. Keyed by whatever aligner default_align_params
                # resolves to in this environment (bwa-mem2 when available,
                # else minimap2), so both keys are marked present.
                AsyncMock(
                    return_value={"bwa-mem2": True, "minimap2": True, "fai": True}
                ),
            ),
            patch(
                "app.pipelines.resource_estimator.classify",
                lambda **kwargs: resource_estimator.Band.OK,
            ),
            patch(
                "app.services.memory_estimate.resolve",
                AsyncMock(return_value=_memory_estimate(2048)),
            ),
            patch(
                "app.services.pipeline_service._resolve_readable",
                AsyncMock(return_value=(None, None)),
            ),
            patch(
                "app.services.pipeline_service.sidecar_payload",
                AsyncMock(return_value={}),
            ),
            patch(
                "app.services.run_service.create_run",
                AsyncMock(return_value=SimpleNamespace(id="run1", owner="local")),
            ),
            patch("app.services.run_service.link_job", AsyncMock()),
            patch("app.queue.queue.enqueue", _enqueue),
        ):
            job = await pipeline_service.launch_alignment(
                object_id=reads.id,
                reference_id=reference.id,
                owner="local",
                paired=False,
            )

        assert job is not None
        assert enqueue_calls == ["align_reads"]


def test_a_child_job_never_reaches_the_block_check():
    """Acceptance criterion: jobs with a parent_job_id never render a card.

    Already true, and NOT implemented by a guard -- parent_job_id is set
    inside queue.enqueue() by callers in queue/results.py, while the BLOCK
    checks live in the launchers, reached only from the API where no
    parent_job_id is ever passed. Pinned here so a future refactor that
    routes child jobs through a launcher fails loudly.
    """
    import inspect

    for name in ("launch_alignment", "launch_assembly"):
        sig = inspect.signature(getattr(pipeline_service, name))
        assert "parent_job_id" not in sig.parameters, (
            f"{name} now accepts parent_job_id -- child jobs can reach the "
            "BLOCK check, and the refusal card would be addressed to an "
            "empty room. Add an explicit skip."
        )
