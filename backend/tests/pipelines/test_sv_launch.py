from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from beanie import PydanticObjectId

from app.errors import ValidationError
from app.pipelines import sniffles_runner
from app.pipelines.align_runner import ReadChemistry
from app.services import pipeline_service


def test_short_read_bam_reaches_delly_at_launch():
    """The gate belongs at launch, not only on the card.

    A card is a suggestion; the endpoint is reachable directly. Before #620
    this asserted short reads were refused outright. They are now routed to
    Delly instead -- but the launch path must still refuse a chemistry no
    caller covers, which `test_unknown_chemistry_is_refused_at_launch`
    below pins.
    """
    from app.pipelines import sv_caller

    assert sniffles_runner.sv_calling_allowed_for(ReadChemistry.SHORT) is True
    assert (
        sv_caller.caller_for_chemistry(ReadChemistry.SHORT)
        is sv_caller.SvCaller.DELLY
    )


def test_unknown_chemistry_is_refused_at_launch():
    """UNKNOWN means QC has not run. This is the gate the test above used
    to provide for short reads, and it must not be lost in the swap."""
    from app.pipelines import sv_caller

    assert sniffles_runner.sv_calling_allowed_for(ReadChemistry.UNKNOWN) is False
    assert sv_caller.caller_for_chemistry(ReadChemistry.UNKNOWN) is None


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


class TestCallerDispatch:
    """The handler must report which caller actually ran."""

    def test_delly_params_are_used_for_a_short_read_payload(self):
        """The handler picks the params class matching the caller. Parsing a
        Delly payload with SnifflesParams would silently accept
        `min_sv_length`, a knob Delly has no flag for."""
        from app.pipelines import delly_runner, sv_caller

        caller = sv_caller.caller_for_chemistry(ReadChemistry.SHORT)
        assert caller is sv_caller.SvCaller.DELLY

        params = delly_runner.DellyParams.from_dict({"threads": 8})
        assert params.threads == 8
        assert not hasattr(params, "min_sv_length")

    def test_provenance_round_trips_the_handler_result(self):
        """The contract between the handler's result dict and
        sv_provenance. Without the caller key, every Delly VCF is stamped as
        Sniffles output -- see Task 4."""
        from app.pipelines.sv_caller import SvCaller
        from app.queue.results import sv_provenance

        result = {"caller": SvCaller.DELLY.value, "tool_version": "2.6.0"}
        assert sv_provenance(result)["variants_called_by"] == "delly"
