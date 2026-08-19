"""launch_feature_coverage: budget refusal, sidecar preconditions, and the
exact enqueue payload feature_coverage_handlers.run_feature_coverage reads.

Mirrors test_declared_budget_refusal.py's SimpleNamespace/patch style, the
established pattern in this file's neighborhood for a DB-touching launch
function (see test_annotation_stats_reference_wiring.py for the same shape
via monkeypatch instead of unittest.mock.patch).

feature_coverage has no tools.require gate in the launch function itself --
unlike launch_variant_calling/launch_quantify, which check a caller tool
before enqueueing, feature_coverage_handlers.run_feature_coverage calls
`tools.require(tools.bedtools())` inside the handler at execution time (see
that module, line ~44). Adding a redundant check here would duplicate a gate
the handler already owns, so none of these tests patch tools.require.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from beanie import PydanticObjectId

from app.errors import ConflictError, ValidationError
from app.models import FormatKind, ObjectStatus
from app.services import pipeline_service


def _budget_of(mb: int):
    async def _budget() -> int:
        return mb

    return _budget


def _bam(*, project_id=None, facts=None, kind=FormatKind.BAM, status=ObjectStatus.READY):
    return SimpleNamespace(
        id=PydanticObjectId(),
        name="aligned.bam",
        format=SimpleNamespace(kind=kind),
        status=status,
        role=None,
        facts=facts if facts is not None else {"sort_order": "coordinate"},
        metadata={},
        blob_sha256="a" * 64,
        project_id=project_id or PydanticObjectId(),
        owner="t",
    )


def _reference(*, project_id=None):
    return SimpleNamespace(
        id=PydanticObjectId(),
        name="reference.fasta",
        format=SimpleNamespace(kind=FormatKind.FASTA),
        status=ObjectStatus.READY,
        role=None,
        facts={},
        metadata={},
        blob_sha256="b" * 64,
        project_id=project_id or PydanticObjectId(),
        owner="t",
    )


def _annotation(*, project_id=None, kind=FormatKind.GFF):
    return SimpleNamespace(
        id=PydanticObjectId(),
        name="genes.gff",
        format=SimpleNamespace(kind=kind),
        status=ObjectStatus.READY,
        role=None,
        facts={},
        metadata={},
        blob_sha256="c" * 64,
        project_id=project_id or PydanticObjectId(),
        owner="t",
    )


def _sidecar(role_marker, *, blob_sha256="d" * 64):
    return SimpleNamespace(
        id=PydanticObjectId(),
        blob_sha256=blob_sha256,
        locality=None,
    )


@pytest.mark.asyncio
async def test_refuses_over_budget_before_touching_the_database(monkeypatch):
    monkeypatch.setattr(
        pipeline_service, "current_admission_budget_mb", _budget_of(500)
    )
    get_object = AsyncMock(side_effect=AssertionError("should not be called"))
    with patch("app.services.object_service.get_object", get_object):
        with pytest.raises(ValidationError) as excinfo:
            await pipeline_service.launch_feature_coverage(
                bam_id=PydanticObjectId(), owner="t", resource_override=False
            )
    assert excinfo.value.details["refusal"] == "declared"
    get_object.assert_not_called()


@pytest.mark.asyncio
async def test_rejects_non_bam_object():
    non_bam = _bam(kind=FormatKind.FASTQ)
    with (
        patch(
            "app.services.pipeline_service.current_admission_budget_mb",
            _budget_of(999_999),
        ),
        patch(
            "app.services.object_service.get_object",
            AsyncMock(return_value=non_bam),
        ),
    ):
        with pytest.raises(ValidationError, match="not a BAM alignment"):
            await pipeline_service.launch_feature_coverage(
                bam_id=non_bam.id, owner="t"
            )


@pytest.mark.asyncio
async def test_missing_bai_refuses_with_index_it_first_message():
    bam = _bam()
    reference = _reference(project_id=bam.project_id)

    async def fake_sidecar_of_role(obj, role):
        return None  # neither .bai nor .fai present

    with (
        patch(
            "app.services.pipeline_service.current_admission_budget_mb",
            _budget_of(999_999),
        ),
        patch(
            "app.services.object_service.get_object",
            AsyncMock(return_value=bam),
        ),
        patch(
            "app.services.pipeline_service._resolve_variant_reference",
            AsyncMock(return_value=reference),
        ),
        patch(
            "app.services.pipeline_service.resolve_annotation",
            AsyncMock(return_value=_annotation(project_id=bam.project_id)),
        ),
        patch(
            "app.services.pipeline_service._sidecar_of_role",
            fake_sidecar_of_role,
        ),
    ):
        with pytest.raises(ValidationError, match="Index it first") as excinfo:
            await pipeline_service.launch_feature_coverage(bam_id=bam.id, owner="t")
    assert excinfo.value.details["needs"] == "index_bam"


@pytest.mark.asyncio
async def test_missing_fai_refuses_with_build_its_index_first_message():
    bam = _bam()
    reference = _reference(project_id=bam.project_id)
    bai = _sidecar("bai")

    from app.models import SidecarRole

    async def fake_sidecar_of_role(obj, role):
        if role is SidecarRole.BAI:
            return bai
        return None  # .fai missing

    with (
        patch(
            "app.services.pipeline_service.current_admission_budget_mb",
            _budget_of(999_999),
        ),
        patch(
            "app.services.object_service.get_object",
            AsyncMock(return_value=bam),
        ),
        patch(
            "app.services.pipeline_service._resolve_variant_reference",
            AsyncMock(return_value=reference),
        ),
        patch(
            "app.services.pipeline_service.resolve_annotation",
            AsyncMock(return_value=_annotation(project_id=bam.project_id)),
        ),
        patch(
            "app.services.pipeline_service._sidecar_of_role",
            fake_sidecar_of_role,
        ),
    ):
        with pytest.raises(
            ValidationError, match="Build its index first"
        ) as excinfo:
            await pipeline_service.launch_feature_coverage(bam_id=bam.id, owner="t")
    assert excinfo.value.details["needs"] == "build_index"


@pytest.mark.asyncio
async def test_resolves_lone_annotation_and_enqueues_exact_payload_keys():
    """Confirms the payload dict literally matches the keys Task 6's
    run_feature_coverage handler reads via ctx.payload.get(...) /
    _resolve_blob(payload, "bam"|"annotation"|"fai")."""
    bam = _bam()
    reference = _reference(project_id=bam.project_id)
    annotation = _annotation(project_id=bam.project_id)
    bai = _sidecar("bai")
    fai = _sidecar("fai")

    from app.models import SidecarRole

    async def fake_sidecar_of_role(obj, role):
        if role is SidecarRole.BAI:
            return bai
        if role is SidecarRole.FAI:
            return fai
        return None

    async def fake_resolve_readable(obj):
        if obj is bam:
            return "a" * 64, None
        if obj is annotation:
            return "c" * 64, None
        if obj is fai:
            return None, "/tmp/reference.fa.fai"
        return None, None

    captured = {}

    async def fake_enqueue(job_type, **kwargs):
        captured["job_type"] = job_type
        captured.update(kwargs)
        return SimpleNamespace(id=PydanticObjectId())

    with (
        patch(
            "app.services.pipeline_service.current_admission_budget_mb",
            _budget_of(999_999),
        ),
        patch(
            "app.services.object_service.get_object",
            AsyncMock(return_value=bam),
        ),
        patch(
            "app.services.pipeline_service._resolve_variant_reference",
            AsyncMock(return_value=reference),
        ),
        patch(
            "app.services.pipeline_service.resolve_annotation",
            AsyncMock(return_value=annotation),
        ) as resolve_annotation_mock,
        patch(
            "app.services.pipeline_service._sidecar_of_role",
            fake_sidecar_of_role,
        ),
        patch(
            "app.services.pipeline_service._resolve_readable",
            fake_resolve_readable,
        ),
        patch("app.queue.queue.enqueue", fake_enqueue),
    ):
        job = await pipeline_service.launch_feature_coverage(
            bam_id=bam.id, owner="t"
        )

    # annotation_id omitted -> resolve_annotation called with annotation_id=None
    resolve_annotation_mock.assert_awaited_once_with(
        bam.project_id, None, owner="t"
    )

    assert job is not None
    assert captured["job_type"] == "feature_coverage"
    payload = captured["payload"]
    assert payload == {
        "bam_id": str(bam.id),
        "bam_name": bam.name,
        "annotation_id": str(annotation.id),
        "annotation_name": annotation.name,
        "annotation_format": "gff",
        "project_id": str(bam.project_id),
        "bam_sha256": "a" * 64,
        "annotation_sha256": "c" * 64,
        "fai_path": "/tmp/reference.fa.fai",
    }
    assert captured["dedup_key"] == (
        f"feature_coverage:{bam.blob_sha256}:{annotation.blob_sha256}"
    )
    assert captured["resources"].mem_mb == pipeline_service.FEATURE_COVERAGE_MEM_MB


@pytest.mark.asyncio
async def test_refuses_gtf_annotation_not_in_the_format_map():
    """_FEATURE_COVERAGE_ANNOTATION_FORMATS deliberately excludes
    FormatKind.GTF (see the comment above that dict in pipeline_service.py):
    _is_annotation accepts GFF, BED, and GTF, so a project whose only
    annotation is a GTF resolves here and must be refused rather than
    silently mapped to the wrong bedtools flavor."""
    bam = _bam()
    reference = _reference(project_id=bam.project_id)
    annotation = _annotation(project_id=bam.project_id, kind=FormatKind.GTF)
    bai = _sidecar("bai")
    fai = _sidecar("fai")

    from app.models import SidecarRole

    async def fake_sidecar_of_role(obj, role):
        if role is SidecarRole.BAI:
            return bai
        if role is SidecarRole.FAI:
            return fai
        return None

    with (
        patch(
            "app.services.pipeline_service.current_admission_budget_mb",
            _budget_of(999_999),
        ),
        patch(
            "app.services.object_service.get_object",
            AsyncMock(return_value=bam),
        ),
        patch(
            "app.services.pipeline_service._resolve_variant_reference",
            AsyncMock(return_value=reference),
        ),
        patch(
            "app.services.pipeline_service.resolve_annotation",
            AsyncMock(return_value=annotation),
        ),
        patch(
            "app.services.pipeline_service._sidecar_of_role",
            fake_sidecar_of_role,
        ),
    ):
        with pytest.raises(ValidationError, match="not GTF") as excinfo:
            await pipeline_service.launch_feature_coverage(bam_id=bam.id, owner="t")
    assert excinfo.value.details["kind"] == "gtf"


@pytest.mark.asyncio
async def test_dedup_collision_raises_conflict_error():
    bam = _bam()
    reference = _reference(project_id=bam.project_id)
    annotation = _annotation(project_id=bam.project_id)
    bai = _sidecar("bai")
    fai = _sidecar("fai")

    from app.models import SidecarRole

    async def fake_sidecar_of_role(obj, role):
        if role is SidecarRole.BAI:
            return bai
        if role is SidecarRole.FAI:
            return fai
        return None

    async def fake_enqueue(job_type, **kwargs):
        return None  # deduplicated away

    with (
        patch(
            "app.services.pipeline_service.current_admission_budget_mb",
            _budget_of(999_999),
        ),
        patch(
            "app.services.object_service.get_object",
            AsyncMock(return_value=bam),
        ),
        patch(
            "app.services.pipeline_service._resolve_variant_reference",
            AsyncMock(return_value=reference),
        ),
        patch(
            "app.services.pipeline_service.resolve_annotation",
            AsyncMock(return_value=annotation),
        ),
        patch(
            "app.services.pipeline_service._sidecar_of_role",
            fake_sidecar_of_role,
        ),
        patch(
            "app.services.pipeline_service._resolve_readable",
            AsyncMock(return_value=(None, None)),
        ),
        patch("app.queue.queue.enqueue", fake_enqueue),
    ):
        with pytest.raises(ConflictError):
            await pipeline_service.launch_feature_coverage(
                bam_id=bam.id, owner="t"
            )
