"""The one place chemistry chooses an SV caller."""

import pytest

from app.pipelines.align_runner import ReadChemistry
from app.pipelines.sv_caller import SvCaller, caller_for_chemistry

_LONG_READ = (
    ReadChemistry.HIFI,
    ReadChemistry.CLR,
    ReadChemistry.ONT_SIMPLEX,
    ReadChemistry.ONT_DUPLEX,
)


@pytest.mark.parametrize("chemistry", _LONG_READ)
def test_long_read_chemistries_get_sniffles(chemistry):
    assert caller_for_chemistry(chemistry) is SvCaller.SNIFFLES2


def test_short_reads_get_delly():
    assert caller_for_chemistry(ReadChemistry.SHORT) is SvCaller.DELLY


def test_unknown_gets_no_caller():
    """UNKNOWN means QC has not run. Guessing wrong in either direction
    produces a junk callset with nothing saying so."""
    assert caller_for_chemistry(ReadChemistry.UNKNOWN) is None


@pytest.mark.parametrize("chemistry", _LONG_READ)
def test_delly_is_never_chosen_for_long_reads(chemistry):
    """Delly 2.6.0 ships a `delly lr` long-read mode that this pipeline
    deliberately does not use -- Sniffles2 produces the .snf sidecar the
    merge card depends on. Requirement SV-620-4."""
    assert caller_for_chemistry(chemistry) is not SvCaller.DELLY


def test_every_chemistry_is_classified():
    """Exhaustiveness: a new ReadChemistry member must be given a caller or
    an explicit None, not silently fall through."""
    for chemistry in ReadChemistry:
        result = caller_for_chemistry(chemistry)
        assert result is None or isinstance(result, SvCaller)
