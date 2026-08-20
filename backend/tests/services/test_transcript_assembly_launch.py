"""launch_transcript_assembly's format gate.

_check_quantifiable is shared with launch_quantify (featureCounts), which can
read BAM, SAM, and CRAM. StringTie 2.2.1 only reads BAM. The Actions-tab card
only ever offers BAM objects, but POST /pipelines/transcript-assembly is
directly reachable with any bam_id, so launch_transcript_assembly must reject
SAM/CRAM itself rather than letting a job burn a slot and fail late inside
the StringTie subprocess.

Mirrors test_feature_coverage_launch.py's SimpleNamespace/patch style for a
DB-touching launch function whose queue path we never want to reach in these
tests -- the format check must fire before annotation resolution or
enqueueing.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from beanie import PydanticObjectId

from app.errors import ValidationError
from app.models import FormatKind, ObjectStatus
from app.services import pipeline_service


def _budget_of(mb: int):
    async def _budget() -> int:
        return mb

    return _budget


def _bam(*, kind=FormatKind.BAM, project_id=None):
    return SimpleNamespace(
        id=PydanticObjectId(),
        name="aligned.sam" if kind == FormatKind.SAM else "aligned.bam",
        format=SimpleNamespace(kind=kind),
        status=ObjectStatus.READY,
        role=None,
        facts={},
        metadata={},
        blob_sha256="a" * 64,
        project_id=project_id or PydanticObjectId(),
        owner="t",
    )


@pytest.mark.asyncio
async def test_rejects_sam_before_enqueueing():
    sam = _bam(kind=FormatKind.SAM)
    resolve_annotation = AsyncMock(side_effect=AssertionError("should not be called"))
    enqueue = AsyncMock(side_effect=AssertionError("should not be called"))
    with (
        patch(
            "app.services.pipeline_service.current_admission_budget_mb",
            _budget_of(999_999),
        ),
        patch(
            "app.services.object_service.get_object",
            AsyncMock(return_value=sam),
        ),
        patch("app.services.pipeline_service.resolve_annotation", resolve_annotation),
        patch("app.queue.queue.enqueue", enqueue),
    ):
        with pytest.raises(ValidationError, match="not BAM"):
            await pipeline_service.launch_transcript_assembly(
                bam_id=sam.id, owner="t"
            )
    resolve_annotation.assert_not_called()
    enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_rejects_cram_before_enqueueing():
    cram = _bam(kind=FormatKind.CRAM)
    with (
        patch(
            "app.services.pipeline_service.current_admission_budget_mb",
            _budget_of(999_999),
        ),
        patch(
            "app.services.object_service.get_object",
            AsyncMock(return_value=cram),
        ),
    ):
        with pytest.raises(ValidationError, match="not BAM"):
            await pipeline_service.launch_transcript_assembly(
                bam_id=cram.id, owner="t"
            )
