"""Pure-function tests for `pipeline_service` helpers that don't need DB
fixtures. Launch-path integration tests live in their own per-feature files
(`test_assembly_error_qc_launch.py`, `test_continuity_qc_launch.py`, ...),
matching this codebase's convention.
"""

import pytest

from app.pipelines.align_runner import ReadChemistry


@pytest.mark.parametrize(
    "chemistry,expected_slot",
    [
        (ReadChemistry.HIFI, "hifi"),
        (ReadChemistry.ONT_SIMPLEX, "nano"),
        (ReadChemistry.ONT_DUPLEX, "nano"),
    ],
)
def test_gci_slot_for_chemistry(chemistry, expected_slot):
    from app.services.pipeline_service import gci_slot_for_chemistry

    assert gci_slot_for_chemistry(chemistry) == expected_slot


def test_gci_slot_refuses_clr():
    """PacBio CLR is long-read and so looks eligible, but GCI has only
    --hifi and --nano and CLR's error profile is nothing like HiFi's.
    Routing it to --hifi would mislabel the evidence; GCI's filters assume
    HiFi-grade per-read accuracy. Refusing is correct.
    """
    from app.services.pipeline_service import gci_slot_for_chemistry

    assert gci_slot_for_chemistry(ReadChemistry.CLR) is None


def test_gci_slot_refuses_short_and_unknown():
    """SHORT has no slot at all -- GCI takes no short-read input. UNKNOWN
    must be refused rather than defaulted: read_chemistry_for_alignment's
    docstring says callers fall back to a conservative short-read default,
    which is right for picking an alignment preset and wrong here.

    Both the UNKNOWN member and a plain None are tested: the enum has a
    real UNKNOWN member, and the resolver can also return None when no
    chemistry was recorded at all. They are different inputs reaching the
    same refusal, and a routing function that handles one but not the
    other passes half of this test in production.
    """
    from app.services.pipeline_service import gci_slot_for_chemistry

    assert gci_slot_for_chemistry(ReadChemistry.SHORT) is None
    assert gci_slot_for_chemistry(ReadChemistry.UNKNOWN) is None
    assert gci_slot_for_chemistry(None) is None
