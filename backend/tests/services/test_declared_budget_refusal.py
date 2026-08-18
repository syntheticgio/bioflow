"""Launch-time refusal of a job that could never be claimed (#478).

The bug: a job whose declared mem_mb exceeds the admission budget can never
satisfy claim.lua's `mem <= mem_free`, has no starvation escape, and no
timeout -- so it waits forever. These tests pin the refusal that replaces
that wait.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from beanie import PydanticObjectId

from app.errors import ValidationError
from app.models import FormatKind, ObjectStatus
from app.pipelines import resource_estimator
from app.services import pipeline_service, resource_limit_service


def _budget_of(mb: int):
    """A stand-in for current_admission_budget_mb, which reads the database."""

    async def _budget() -> int:
        return mb

    return _budget


def _no_tool_check():
    """Stub for tools.require -- the test image is arm64 and several tools
    these launchers require (polypolish, bwa-mem2, ...) have no linux-aarch64
    build, so the real check fails before the launcher can reach enqueue.
    Unrelated to the budget refusal under test.
    """
    return patch.object(pipeline_service.tools, "require", lambda tool: tool)


def _annotate_genome_fixture():
    """A ready, bacterial-FASTA assembly, shaped like launch_annotate_genome
    expects to get past its own validation (organism, format, readability)
    before the enqueue call.
    """
    return SimpleNamespace(
        id=PydanticObjectId(),
        name="assembly.fasta",
        format=SimpleNamespace(kind=FormatKind.FASTA),
        status=ObjectStatus.READY,
        role=None,
        metadata={"organism": "Escherichia coli"},
        facts={},
        blob_sha256="a" * 64,
        project_id=PydanticObjectId(),
        owner="t",
    )


def test_over_budget_declaration_raises():
    """R1: a declaration above the budget is refused, not queued."""
    with pytest.raises(ValidationError) as excinfo:
        pipeline_service.refuse_if_over_budget(
            declared_mb=16384, budget_mb=5600, resource_override=False
        )
    assert "16,384" in str(excinfo.value)
    assert "5,600" in str(excinfo.value)


def test_override_skips_the_refusal():
    """R3: 'Launch anyway' proceeds; claim.lua admits it under sole occupancy."""
    pipeline_service.refuse_if_over_budget(
        declared_mb=16384, budget_mb=5600, resource_override=True
    )


def test_within_budget_declaration_is_unaffected():
    """R6: the regression guard -- normal jobs see no new refusal."""
    pipeline_service.refuse_if_over_budget(
        declared_mb=2048, budget_mb=5600, resource_override=False
    )


def test_equal_to_budget_is_allowed():
    """claim.lua admits on `mem <= mem_free`, so equality must fit here too."""
    pipeline_service.refuse_if_over_budget(
        declared_mb=5600, budget_mb=5600, resource_override=False
    )


def test_unknown_assembly_declaration_exceeds_a_modest_budget():
    """R6a: the case with no estimate at all.

    An assembly nothing can estimate declares UNKNOWN_ASSEMBLY_MEM_MB and is
    banded by nothing -- both launch sites guard their banding on
    `estimate is not None`. This asserts the value is genuinely over a modest
    budget, which is what makes placing the check outside that guard load-bearing.
    """
    budget = resource_limit_service.admission_budget_mb(
        stored_mb=8000, machine_mb=32000
    )
    assert resource_estimator.exceeds_declared_budget(
        declared_mb=pipeline_service.UNKNOWN_ASSEMBLY_MEM_MB, budget_mb=budget
    )
    with pytest.raises(ValidationError):
        pipeline_service.refuse_if_over_budget(
            declared_mb=pipeline_service.UNKNOWN_ASSEMBLY_MEM_MB,
            budget_mb=budget,
            resource_override=False,
        )


def test_min_declared_floor_can_exceed_a_small_budget():
    """Spec case 2: the floor lifts a declaration past a banded-OK estimate.

    `declared_align_mem_mb` floors at MIN_DECLARED_MEM_MB, so a tiny alignment
    still declares 2048 MB. Under a very small budget that is unclaimable,
    while the estimate the banding saw was smaller and passed.
    """
    tiny_budget = resource_limit_service.admission_budget_mb(
        stored_mb=1024, machine_mb=32000
    )
    assert resource_estimator.exceeds_declared_budget(
        declared_mb=pipeline_service.MIN_DECLARED_MEM_MB, budget_mb=tiny_budget
    )
    with pytest.raises(ValidationError):
        pipeline_service.refuse_if_over_budget(
            declared_mb=pipeline_service.MIN_DECLARED_MEM_MB,
            budget_mb=tiny_budget,
            resource_override=False,
        )


def test_declared_refusal_is_tagged_for_the_frontend():
    """R4: the card is routed by this key, not by sniffing estimate_mb.

    #478 shipped without it, so AssembleDialog's `"estimate_mb" in details`
    guard was false for every declared refusal and the escape hatch the
    message promises never rendered.
    """
    with pytest.raises(ValidationError) as excinfo:
        pipeline_service.refuse_if_over_budget(
            declared_mb=16384, budget_mb=5600, resource_override=False
        )
    assert excinfo.value.details["refusal"] == "declared"
    assert excinfo.value.details["declared_mb"] == 16384
    assert excinfo.value.details["budget_mb"] == 5600


@pytest.mark.asyncio
async def test_annotate_genome_refuses_over_budget(monkeypatch):
    """R1: the issue's headline case -- 16384 MB against a smaller budget."""
    monkeypatch.setattr(
        pipeline_service, "current_admission_budget_mb", _budget_of(5600)
    )
    with pytest.raises(ValidationError) as excinfo:
        await pipeline_service.launch_annotate_genome(
            object_id=PydanticObjectId(), owner="t", resource_override=False
        )
    assert excinfo.value.details["refusal"] == "declared"


@pytest.mark.asyncio
async def test_annotate_genome_override_enqueues_with_the_flag(monkeypatch):
    """R2: 'Launch anyway' reaches the job, where claim.lua reads it."""
    monkeypatch.setattr(
        pipeline_service, "current_admission_budget_mb", _budget_of(5600)
    )
    obj = _annotate_genome_fixture()
    captured = {}

    async def _fake_enqueue(job_type, **kwargs):
        captured.update(kwargs)
        return None  # the launcher raises ConflictError; we only need the call

    # bakta isn't installed in the test image, and the real object lookup
    # hits an empty DB -- both are unrelated to the budget check, so they're
    # stubbed out to let the launcher actually reach the (patched) enqueue.
    with (
        patch("app.queue.queue.enqueue", _fake_enqueue),
        _no_tool_check(),
        patch(
            "app.services.object_service.get_object",
            AsyncMock(return_value=obj),
        ),
        patch(
            "app.services.pipeline_service._resolve_readable",
            AsyncMock(return_value=("a" * 64, None)),
        ),
    ):
        with pytest.raises(Exception):
            await pipeline_service.launch_annotate_genome(
                object_id=obj.id, owner="t", resource_override=True
            )
    assert captured["resource_override"] is True


@pytest.mark.asyncio
async def test_polish_refuses_over_budget(monkeypatch):
    monkeypatch.setattr(
        pipeline_service, "current_admission_budget_mb", _budget_of(5600)
    )
    with pytest.raises(ValidationError) as excinfo:
        await pipeline_service.launch_polish(
            draft_object_id=PydanticObjectId(), owner="t", resource_override=False
        )
    assert excinfo.value.details["refusal"] == "declared"


def _short_read_fixture(project_id):
    return SimpleNamespace(
        id=PydanticObjectId(),
        name="reads.fastq",
        format=SimpleNamespace(kind=FormatKind.FASTQ),
        status=ObjectStatus.READY,
        role=None,
        facts={},
        metadata={},
        blob_sha256="b" * 64,
        project_id=project_id,
        owner="t",
        size=1_000_000,
    )


def _draft_assembly_fixture():
    project_id = PydanticObjectId()
    return SimpleNamespace(
        id=PydanticObjectId(),
        name="draft.fasta",
        format=SimpleNamespace(kind=FormatKind.FASTA),
        status=ObjectStatus.READY,
        role=None,
        facts={},
        metadata={},
        blob_sha256="c" * 64,
        project_id=project_id,
        owner="t",
        size=5_000_000,
    )


@pytest.mark.asyncio
async def test_polish_override_enqueues_with_the_flag(monkeypatch):
    monkeypatch.setattr(
        pipeline_service, "current_admission_budget_mb", _budget_of(5600)
    )
    draft = _draft_assembly_fixture()
    reads = _short_read_fixture(draft.project_id)
    captured = {}

    async def _fake_enqueue(job_type, **kwargs):
        captured.update(kwargs)
        return None

    with (
        patch("app.queue.queue.enqueue", _fake_enqueue),
        _no_tool_check(),
        patch(
            "app.services.object_service.get_object",
            AsyncMock(side_effect=[draft, reads]),
        ),
        patch(
            "app.services.reference_assembly.check_draft_assembly",
            lambda obj: obj,
        ),
        patch(
            "app.services.reference_assembly.is_short_read",
            lambda obj: True,
        ),
        patch(
            "app.services.pipeline_service._resolve_readable",
            AsyncMock(return_value=("d" * 64, None)),
        ),
        patch(
            "app.services.run_service.create_run",
            AsyncMock(return_value=SimpleNamespace(id="run1", owner="t")),
        ),
    ):
        with pytest.raises(Exception):
            await pipeline_service.launch_polish(
                draft_object_id=draft.id,
                owner="t",
                reads_object_id=reads.id,
                resource_override=True,
            )
    assert captured["resource_override"] is True

@pytest.mark.asyncio
async def test_qv_qc_refuses_over_budget(monkeypatch):
    monkeypatch.setattr(
        pipeline_service, "current_admission_budget_mb", _budget_of(5600)
    )
    with pytest.raises(ValidationError) as excinfo:
        await pipeline_service.launch_qv_qc(
            PydanticObjectId(), owner="t", resource_override=False
        )
    assert excinfo.value.details["refusal"] == "declared"


@pytest.mark.asyncio
async def test_qv_qc_override_enqueues_with_the_flag(monkeypatch):
    monkeypatch.setattr(
        pipeline_service, "current_admission_budget_mb", _budget_of(5600)
    )
    assembly = _draft_assembly_fixture()
    reads = _short_read_fixture(assembly.project_id)
    captured = {}

    async def _fake_enqueue(job_type, **kwargs):
        captured.update(kwargs)
        return None

    with (
        patch("app.queue.queue.enqueue", _fake_enqueue),
        _no_tool_check(),
        patch(
            "app.services.object_service.get_object",
            AsyncMock(side_effect=[assembly, reads]),
        ),
        patch(
            "app.services.reference_assembly.check_draft_assembly",
            lambda obj: obj,
        ),
        patch(
            "app.services.pipeline_service._materialize_meryl_cache",
            AsyncMock(return_value=None),
        ),
        patch(
            "app.services.pipeline_service._resolve_readable",
            AsyncMock(return_value=("e" * 64, None)),
        ),
    ):
        with pytest.raises(Exception):
            await pipeline_service.launch_qv_qc(
                assembly.id,
                owner="t",
                read_object_id=reads.id,
                resource_override=True,
            )
    assert captured["resource_override"] is True

@pytest.mark.asyncio
async def test_continuity_qc_refuses_over_budget(monkeypatch):
    monkeypatch.setattr(
        pipeline_service, "current_admission_budget_mb", _budget_of(5600)
    )
    with pytest.raises(ValidationError) as excinfo:
        await pipeline_service.launch_continuity_qc(
            object_id=PydanticObjectId(), owner="t", resource_override=False
        )
    assert excinfo.value.details["refusal"] == "declared"


@pytest.mark.asyncio
async def test_continuity_qc_override_enqueues_with_the_flag(monkeypatch):
    monkeypatch.setattr(
        pipeline_service, "current_admission_budget_mb", _budget_of(5600)
    )
    assembly = _draft_assembly_fixture()
    hifi_bam = SimpleNamespace(
        id=PydanticObjectId(),
        name="hifi.bam",
        format=SimpleNamespace(kind=FormatKind.BAM),
        status=ObjectStatus.READY,
        role=None,
        facts={"aligned_by": "minimap2"},
        metadata={},
        blob_sha256="f" * 64,
        project_id=assembly.project_id,
        owner="t",
        derived_from=[assembly.id],
    )
    captured = {}

    async def _fake_enqueue(job_type, **kwargs):
        captured.update(kwargs)
        return None

    with (
        patch("app.queue.queue.enqueue", _fake_enqueue),
        _no_tool_check(),
        patch(
            "app.services.object_service.get_object",
            AsyncMock(side_effect=[assembly, hifi_bam]),
        ),
        patch(
            "app.services.reference_assembly.check_draft_assembly",
            lambda obj: obj,
        ),
        patch(
            "app.services.pipeline_service.read_chemistry_for_alignment",
            AsyncMock(return_value=None),
        ),
        patch(
            "app.services.pipeline_service.gci_slot_for_chemistry",
            lambda chemistry: "hifi",
        ),
        patch(
            "app.services.pipeline_service._resolve_readable",
            AsyncMock(return_value=("g" * 64, None)),
        ),
        patch(
            "app.services.pipeline_service._sidecar_of_role",
            AsyncMock(return_value=None),
        ),
    ):
        with pytest.raises(Exception):
            await pipeline_service.launch_continuity_qc(
                object_id=assembly.id,
                owner="t",
                hifi_bam_ids=[hifi_bam.id],
                resource_override=True,
            )
    assert captured["resource_override"] is True

@pytest.mark.asyncio
async def test_variant_calling_refuses_over_budget(monkeypatch):
    monkeypatch.setattr(
        pipeline_service, "current_admission_budget_mb", _budget_of(5600)
    )
    with pytest.raises(ValidationError) as excinfo:
        await pipeline_service.launch_variant_calling(
            bam_id=PydanticObjectId(), owner="t", resource_override=False
        )
    assert excinfo.value.details["refusal"] == "declared"


@pytest.mark.asyncio
async def test_variant_calling_override_enqueues_with_the_flag(monkeypatch):
    monkeypatch.setattr(
        pipeline_service, "current_admission_budget_mb", _budget_of(5600)
    )
    project_id = PydanticObjectId()
    bam = SimpleNamespace(
        id=PydanticObjectId(),
        name="reads.bam",
        format=SimpleNamespace(kind=FormatKind.BAM),
        status=ObjectStatus.READY,
        role=None,
        facts={},
        metadata={},
        blob_sha256="h" * 64,
        project_id=project_id,
        owner="t",
        derived_from=[],
    )
    captured = {}

    async def _fake_enqueue(job_type, **kwargs):
        captured.update(kwargs)
        return None

    with (
        patch("app.queue.queue.enqueue", _fake_enqueue),
        _no_tool_check(),
        patch(
            "app.services.object_service.get_object",
            AsyncMock(return_value=bam),
        ),
        patch(
            "app.services.pipeline_service._check_variant_callable",
            lambda obj: None,
        ),
        patch(
            "app.services.pipeline_service.read_chemistry_for_alignment",
            AsyncMock(return_value=None),
        ),
        patch(
            "app.services.pipeline_service._resolve_variant_reference",
            AsyncMock(return_value=bam),
        ),
        patch(
            "app.services.pipeline_service._sidecar_of_role",
            AsyncMock(return_value=bam),
        ),
        patch(
            "app.services.pipeline_service.default_variant_params",
            AsyncMock(return_value={}),
        ),
        patch(
            "app.services.pipeline_service._variant_payload",
            AsyncMock(return_value={}),
        ),
        patch(
            "app.services.run_service.create_run",
            AsyncMock(return_value=SimpleNamespace(id="run1", owner="t")),
        ),
    ):
        with pytest.raises(Exception):
            await pipeline_service.launch_variant_calling(
                bam_id=bam.id, owner="t", resource_override=True
            )
    assert captured["resource_override"] is True

@pytest.mark.asyncio
async def test_completeness_refuses_over_budget(monkeypatch):
    monkeypatch.setattr(
        pipeline_service, "current_admission_budget_mb", _budget_of(5600)
    )
    with pytest.raises(ValidationError) as excinfo:
        await pipeline_service.launch_completeness(
            object_id=PydanticObjectId(), owner="t", resource_override=False
        )
    assert excinfo.value.details["refusal"] == "declared"


@pytest.mark.asyncio
async def test_completeness_override_enqueues_with_the_flag(monkeypatch):
    monkeypatch.setattr(
        pipeline_service, "current_admission_budget_mb", _budget_of(5600)
    )
    obj = _annotate_genome_fixture()
    captured = {}

    async def _fake_enqueue(job_type, **kwargs):
        captured.update(kwargs)
        return None

    with (
        patch("app.queue.queue.enqueue", _fake_enqueue),
        _no_tool_check(),
        patch(
            "app.services.object_service.get_object",
            AsyncMock(return_value=obj),
        ),
        patch(
            "app.pipelines.lineage_inference.infer_lineage",
            lambda organism: "bacteria",
        ),
        patch(
            "app.queue.lineage_handlers.lineage_present",
            lambda *a, **k: True,
        ),
        patch(
            "app.services.pipeline_service._resolve_readable",
            AsyncMock(return_value=("i" * 64, None)),
        ),
    ):
        with pytest.raises(Exception):
            await pipeline_service.launch_completeness(
                object_id=obj.id, owner="t", resource_override=True
            )
    assert captured["resource_override"] is True

@pytest.mark.asyncio
async def test_meryl_analysis_refuses_over_budget(monkeypatch):
    monkeypatch.setattr(
        pipeline_service, "current_admission_budget_mb", _budget_of(5600)
    )
    with pytest.raises(ValidationError) as excinfo:
        await pipeline_service.launch_meryl_analysis(
            object_id=PydanticObjectId(), owner="t", resource_override=False
        )
    assert excinfo.value.details["refusal"] == "declared"


@pytest.mark.asyncio
async def test_meryl_analysis_override_enqueues_with_the_flag(monkeypatch):
    monkeypatch.setattr(
        pipeline_service, "current_admission_budget_mb", _budget_of(5600)
    )
    assembly = _draft_assembly_fixture()
    reads = _short_read_fixture(assembly.project_id)
    captured = {}

    async def _fake_enqueue(job_type, **kwargs):
        captured.update(kwargs)
        return None

    with (
        patch("app.queue.queue.enqueue", _fake_enqueue),
        _no_tool_check(),
        patch(
            "app.services.object_service.get_object",
            AsyncMock(side_effect=[assembly, reads]),
        ),
        patch(
            "app.services.reference_assembly.check_draft_assembly",
            lambda obj: obj,
        ),
        patch(
            "app.services.pipeline_service._materialize_meryl_cache",
            AsyncMock(return_value=None),
        ),
        patch(
            "app.services.pipeline_service._resolve_readable",
            AsyncMock(return_value=("j" * 64, None)),
        ),
    ):
        with pytest.raises(Exception):
            await pipeline_service.launch_meryl_analysis(
                object_id=assembly.id,
                owner="t",
                read_object_id=reads.id,
                resource_override=True,
            )
    assert captured["resource_override"] is True

@pytest.mark.asyncio
async def test_consensus_refuses_over_budget(monkeypatch):
    monkeypatch.setattr(
        pipeline_service, "current_admission_budget_mb", _budget_of(5600)
    )
    with pytest.raises(ValidationError) as excinfo:
        await pipeline_service.launch_consensus(
            bam_object_id=PydanticObjectId(), owner="t", resource_override=False
        )
    assert excinfo.value.details["refusal"] == "declared"


@pytest.mark.asyncio
async def test_consensus_override_enqueues_with_the_flag(monkeypatch):
    monkeypatch.setattr(
        pipeline_service, "current_admission_budget_mb", _budget_of(5600)
    )
    project_id = PydanticObjectId()
    bam = SimpleNamespace(
        id=PydanticObjectId(),
        name="reads.bam",
        format=SimpleNamespace(kind=FormatKind.BAM),
        status=ObjectStatus.READY,
        role=None,
        facts={},
        metadata={},
        blob_sha256="k" * 64,
        project_id=project_id,
        owner="t",
    )
    reference = SimpleNamespace(
        id=PydanticObjectId(),
        name="ref.fasta",
        format=SimpleNamespace(kind=FormatKind.FASTA),
        status=ObjectStatus.READY,
        role=None,
        facts={},
        metadata={},
        blob_sha256="l" * 64,
        project_id=project_id,
        owner="t",
    )
    captured = {}

    async def _fake_enqueue(job_type, **kwargs):
        captured.update(kwargs)
        return None

    with (
        patch("app.queue.queue.enqueue", _fake_enqueue),
        _no_tool_check(),
        patch(
            "app.services.object_service.get_object",
            AsyncMock(return_value=bam),
        ),
        patch(
            "app.services.reference_assembly.resolve_alignment_target_for_bam",
            AsyncMock(return_value=reference),
        ),
        patch(
            "app.services.pipeline_service._resolve_readable",
            AsyncMock(return_value=("m" * 64, None)),
        ),
        patch(
            "app.services.run_service.create_run",
            AsyncMock(return_value=SimpleNamespace(id="run1", owner="t")),
        ),
    ):
        with pytest.raises(Exception):
            await pipeline_service.launch_consensus(
                bam_object_id=bam.id, owner="t", resource_override=True
            )
    assert captured["resource_override"] is True

def _reference_fixture(project_id):
    return SimpleNamespace(
        id=PydanticObjectId(),
        name="ref.fasta",
        format=SimpleNamespace(kind=FormatKind.FASTA),
        status=ObjectStatus.READY,
        role=None,
        facts={},
        metadata={},
        blob_sha256="n" * 64,
        project_id=project_id,
        owner="t",
    )


@pytest.mark.asyncio
async def test_scaffold_refuses_over_budget(monkeypatch):
    monkeypatch.setattr(
        pipeline_service, "current_admission_budget_mb", _budget_of(5600)
    )
    with pytest.raises(ValidationError) as excinfo:
        await pipeline_service.launch_scaffold(
            draft_object_id=PydanticObjectId(), owner="t", resource_override=False
        )
    assert excinfo.value.details["refusal"] == "declared"


@pytest.mark.asyncio
async def test_scaffold_override_enqueues_with_the_flag(monkeypatch):
    monkeypatch.setattr(
        pipeline_service, "current_admission_budget_mb", _budget_of(5600)
    )
    draft = _draft_assembly_fixture()
    reference = _reference_fixture(draft.project_id)
    captured = {}

    async def _fake_enqueue(job_type, **kwargs):
        captured.update(kwargs)
        return None

    with (
        patch("app.queue.queue.enqueue", _fake_enqueue),
        _no_tool_check(),
        patch(
            "app.services.object_service.get_object",
            AsyncMock(side_effect=[draft, reference]),
        ),
        patch(
            "app.services.reference_assembly.check_draft_assembly",
            lambda obj: obj,
        ),
        patch(
            "app.services.reference_assembly.check_reference_assembly",
            lambda obj: obj,
        ),
        patch(
            "app.services.pipeline_service._resolve_readable",
            AsyncMock(return_value=("o" * 64, None)),
        ),
        patch(
            "app.services.run_service.create_run",
            AsyncMock(return_value=SimpleNamespace(id="run1", owner="t")),
        ),
    ):
        with pytest.raises(Exception):
            await pipeline_service.launch_scaffold(
                draft_object_id=draft.id,
                owner="t",
                reference_object_id=reference.id,
                resource_override=True,
            )
    assert captured["resource_override"] is True


@pytest.mark.asyncio
async def test_misassembly_qc_refuses_over_budget(monkeypatch):
    monkeypatch.setattr(
        pipeline_service, "current_admission_budget_mb", _budget_of(5600)
    )
    with pytest.raises(ValidationError) as excinfo:
        await pipeline_service.launch_misassembly_qc(
            draft_object_id=PydanticObjectId(), owner="t", resource_override=False
        )
    assert excinfo.value.details["refusal"] == "declared"


@pytest.mark.asyncio
async def test_misassembly_qc_override_enqueues_with_the_flag(monkeypatch):
    monkeypatch.setattr(
        pipeline_service, "current_admission_budget_mb", _budget_of(5600)
    )
    draft = _draft_assembly_fixture()
    reference = _reference_fixture(draft.project_id)
    captured = {}

    async def _fake_enqueue(job_type, **kwargs):
        captured.update(kwargs)
        return None

    with (
        patch("app.queue.queue.enqueue", _fake_enqueue),
        _no_tool_check(),
        patch(
            "app.services.object_service.get_object",
            AsyncMock(side_effect=[draft, reference]),
        ),
        patch(
            "app.services.reference_assembly.check_draft_assembly",
            lambda obj: obj,
        ),
        patch(
            "app.services.reference_assembly.check_reference_assembly",
            lambda obj: obj,
        ),
        patch(
            "app.services.pipeline_service._resolve_readable",
            AsyncMock(return_value=("p" * 64, None)),
        ),
    ):
        with pytest.raises(Exception):
            await pipeline_service.launch_misassembly_qc(
                draft_object_id=draft.id,
                owner="t",
                reference_object_id=reference.id,
                resource_override=True,
            )
    assert captured["resource_override"] is True


@pytest.mark.asyncio
async def test_synteny_refuses_over_budget(monkeypatch):
    monkeypatch.setattr(
        pipeline_service, "current_admission_budget_mb", _budget_of(5600)
    )
    with pytest.raises(ValidationError) as excinfo:
        await pipeline_service.launch_synteny(
            draft_object_id=PydanticObjectId(), owner="t", resource_override=False
        )
    assert excinfo.value.details["refusal"] == "declared"


@pytest.mark.asyncio
async def test_synteny_override_enqueues_with_the_flag(monkeypatch):
    monkeypatch.setattr(
        pipeline_service, "current_admission_budget_mb", _budget_of(5600)
    )
    draft = _draft_assembly_fixture()
    reference = _reference_fixture(draft.project_id)
    captured = {}

    async def _fake_enqueue(job_type, **kwargs):
        captured.update(kwargs)
        return None

    with (
        patch("app.queue.queue.enqueue", _fake_enqueue),
        _no_tool_check(),
        patch(
            "app.services.object_service.get_object",
            AsyncMock(side_effect=[draft, reference]),
        ),
        patch(
            "app.services.reference_assembly.check_draft_assembly",
            lambda obj: obj,
        ),
        patch(
            "app.services.reference_assembly.check_reference_assembly",
            lambda obj: obj,
        ),
        patch(
            "app.services.pipeline_service._resolve_readable",
            AsyncMock(return_value=("q" * 64, None)),
        ),
    ):
        with pytest.raises(Exception):
            await pipeline_service.launch_synteny(
                draft_object_id=draft.id,
                owner="t",
                reference_object_id=reference.id,
                resource_override=True,
            )
    assert captured["resource_override"] is True

@pytest.mark.asyncio
async def test_assembly_error_qc_refuses_over_budget(monkeypatch):
    monkeypatch.setattr(
        pipeline_service, "current_admission_budget_mb", _budget_of(5600)
    )
    with pytest.raises(ValidationError) as excinfo:
        await pipeline_service.launch_assembly_error_qc(
            object_id=PydanticObjectId(), owner="t", resource_override=False
        )
    assert excinfo.value.details["refusal"] == "declared"


@pytest.mark.asyncio
async def test_assembly_error_qc_override_enqueues_with_the_flag(monkeypatch):
    monkeypatch.setattr(
        pipeline_service, "current_admission_budget_mb", _budget_of(5600)
    )
    assembly = _draft_assembly_fixture()
    ngs_bam = SimpleNamespace(
        id=PydanticObjectId(),
        name="ngs.bam",
        format=SimpleNamespace(kind=FormatKind.BAM),
        status=ObjectStatus.READY,
        role=None,
        facts={},
        metadata={},
        blob_sha256="r" * 64,
        project_id=assembly.project_id,
        owner="t",
        derived_from=[assembly.id],
    )
    captured = {}

    async def _fake_enqueue(job_type, **kwargs):
        captured.update(kwargs)
        return None

    with (
        patch("app.queue.queue.enqueue", _fake_enqueue),
        _no_tool_check(),
        patch(
            "app.services.object_service.get_object",
            AsyncMock(side_effect=[assembly, ngs_bam]),
        ),
        patch(
            "app.services.reference_assembly.check_draft_assembly",
            lambda obj: obj,
        ),
        patch(
            "app.services.pipeline_service._resolve_readable",
            AsyncMock(return_value=("s" * 64, None)),
        ),
        patch(
            "app.services.pipeline_service._sidecar_of_role",
            AsyncMock(return_value=None),
        ),
    ):
        with pytest.raises(Exception):
            await pipeline_service.launch_assembly_error_qc(
                object_id=assembly.id,
                owner="t",
                ngs_bam_id=ngs_bam.id,
                resource_override=True,
            )
    assert captured["resource_override"] is True

@pytest.mark.asyncio
async def test_quantify_refuses_over_budget(monkeypatch):
    monkeypatch.setattr(
        pipeline_service, "current_admission_budget_mb", _budget_of(1000)
    )
    with pytest.raises(ValidationError) as excinfo:
        await pipeline_service.launch_quantify(
            bam_id=PydanticObjectId(), owner="t", resource_override=False
        )
    assert excinfo.value.details["refusal"] == "declared"


@pytest.mark.asyncio
async def test_quantify_override_enqueues_with_the_flag(monkeypatch):
    monkeypatch.setattr(
        pipeline_service, "current_admission_budget_mb", _budget_of(1000)
    )
    project_id = PydanticObjectId()
    bam = SimpleNamespace(
        id=PydanticObjectId(),
        name="reads.bam",
        format=SimpleNamespace(kind=FormatKind.BAM),
        status=ObjectStatus.READY,
        role=None,
        facts={},
        metadata={},
        blob_sha256="t" * 64,
        project_id=project_id,
        owner="t",
    )
    annotation = SimpleNamespace(
        id=PydanticObjectId(),
        name="genes.gtf",
        format=SimpleNamespace(kind=FormatKind.GTF),
        status=ObjectStatus.READY,
        role=None,
        facts={},
        metadata={},
        blob_sha256="u" * 64,
        project_id=project_id,
        owner="t",
    )
    captured = {}

    async def _fake_enqueue(job_type, **kwargs):
        captured.update(kwargs)
        return None

    with (
        patch("app.queue.queue.enqueue", _fake_enqueue),
        _no_tool_check(),
        patch(
            "app.services.object_service.get_object",
            AsyncMock(return_value=bam),
        ),
        patch(
            "app.services.pipeline_service._check_quantifiable",
            lambda obj: None,
        ),
        patch(
            "app.services.pipeline_service.resolve_annotation",
            AsyncMock(return_value=annotation),
        ),
        patch(
            "app.services.pipeline_service.default_count_params",
            AsyncMock(return_value={}),
        ),
        patch(
            "app.services.pipeline_service._resolve_readable",
            AsyncMock(return_value=("v" * 64, None)),
        ),
        patch(
            "app.services.run_service.create_run",
            AsyncMock(return_value=SimpleNamespace(id="run1", owner="t")),
        ),
    ):
        with pytest.raises(Exception):
            await pipeline_service.launch_quantify(
                bam_id=bam.id, owner="t", resource_override=True
            )
    assert captured["resource_override"] is True
    assert captured["resources"].mem_mb == pipeline_service.QUANTIFY_MEM_MB
