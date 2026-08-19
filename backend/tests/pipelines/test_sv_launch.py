import pytest

from app.errors import ValidationError
from app.pipelines import sniffles_runner
from app.pipelines.align_runner import ReadChemistry


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
