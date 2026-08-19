from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from beanie import PydanticObjectId

from app.errors import ValidationError
from app.pipelines import sniffles_runner
from app.pipelines.align_runner import ReadChemistry
from app.services import pipeline_service


def test_short_read_bam_is_refused_before_a_job_is_queued():
    """The gate belongs at launch, not only on the card.

    A card is a suggestion; the endpoint is reachable directly. Refusing
    only in the UI would let a short-read BAM through the API and produce a
    junk callset with nothing saying so.
    """
    assert sniffles_runner.sv_calling_allowed_for(ReadChemistry.SHORT) is False


def test_params_reject_a_zero_support_threshold():
    with pytest.raises(ValidationError):
        sniffles_runner.SnifflesParams.from_dict({"min_support": 0})


def test_params_round_trip_through_as_dict():
    params = sniffles_runner.SnifflesParams(min_support=5, min_sv_length=100)
    assert sniffles_runner.SnifflesParams.from_dict(params.as_dict()) == params


@pytest.mark.asyncio
async def test_sv_payload_carries_chemistry():
    """Mirrors `_variant_payload`'s chemistry key.

    Without this, `sv_handlers._check_chemistry`'s defense-in-depth
    re-validation at execution time reads `payload.get("chemistry")` as
    always None and never actually re-checks anything -- a payload that
    outlives the launch-time check would sail through with no re-check at
    all.
    """
    bam = SimpleNamespace(id=PydanticObjectId(), project_id=PydanticObjectId(), name="r.bam")
    reference = SimpleNamespace(
        id=PydanticObjectId(), project_id=bam.project_id, name="ref.fna"
    )
    bai = SimpleNamespace(id=PydanticObjectId(), project_id=bam.project_id, name="r.bam.bai")
    fai = SimpleNamespace(id=PydanticObjectId(), project_id=bam.project_id, name="ref.fna.fai")
    params = sniffles_runner.SnifflesParams()

    with patch(
        "app.services.pipeline_service._resolve_readable",
        AsyncMock(return_value=(None, "/data/fake")),
    ):
        payload = await pipeline_service._sv_payload(
            bam=bam,
            reference=reference,
            bai=bai,
            fai=fai,
            chemistry=ReadChemistry.ONT_SIMPLEX,
            params=params,
        )

    assert payload["chemistry"] == ReadChemistry.ONT_SIMPLEX.value


@pytest.mark.asyncio
async def test_sv_payload_omits_chemistry_when_unresolved():
    bam = SimpleNamespace(id=PydanticObjectId(), project_id=PydanticObjectId(), name="r.bam")
    reference = SimpleNamespace(
        id=PydanticObjectId(), project_id=bam.project_id, name="ref.fna"
    )
    bai = SimpleNamespace(id=PydanticObjectId(), project_id=bam.project_id, name="r.bam.bai")
    fai = SimpleNamespace(id=PydanticObjectId(), project_id=bam.project_id, name="ref.fna.fai")
    params = sniffles_runner.SnifflesParams()

    with patch(
        "app.services.pipeline_service._resolve_readable",
        AsyncMock(return_value=(None, "/data/fake")),
    ):
        payload = await pipeline_service._sv_payload(
            bam=bam,
            reference=reference,
            bai=bai,
            fai=fai,
            chemistry=None,
            params=params,
        )

    assert "chemistry" not in payload
