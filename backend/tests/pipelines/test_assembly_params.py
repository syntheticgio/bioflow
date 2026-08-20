import pytest

from app.errors import ValidationError
from app.pipelines import assembly_params
from app.pipelines.assemblers import Assembler


def test_abyss_params_default_k():
    params = assembly_params.from_dict({"assembler": "abyss"})
    assert params.assembler is Assembler.ABYSS
    assert params.k == 51
    assert params.threads == 8


def test_abyss_params_accepts_k():
    params = assembly_params.from_dict({"assembler": "abyss", "k": 31})
    assert params.k == 31


def test_abyss_params_rejects_k_below_floor():
    with pytest.raises(ValidationError, match="k must be between"):
        assembly_params.from_dict({"assembler": "abyss", "k": 4})


def test_abyss_params_rejects_k_above_ceiling():
    with pytest.raises(ValidationError, match="k must be between"):
        assembly_params.from_dict({"assembler": "abyss", "k": 500})


def test_abyss_params_roundtrip_carries_k():
    params = assembly_params.from_dict({"assembler": "abyss", "k": 63})
    assert params.as_dict()["k"] == 63
    assert params.as_dict()["assembler"] == "abyss"


def test_spades_params_now_available():
    """SPAdes params class now exists, replacing the "not installed" state."""
    params = assembly_params.from_dict({"assembler": "spades"})
    assert params.assembler is Assembler.SPADES


class TestFlyeParams:
    def test_meta_defaults_to_false(self):
        params = assembly_params.from_dict({"assembler": "flye"})
        assert params.meta is False

    def test_accepts_meta_true(self):
        params = assembly_params.from_dict({"assembler": "flye", "meta": True})
        assert params.meta is True

    def test_round_trips_through_as_dict(self):
        params = assembly_params.from_dict({"assembler": "flye", "meta": True})
        restored = assembly_params.from_dict(params.as_dict())
        assert restored == params
        assert restored.meta is True


class TestSpadesParams:
    def test_defaults_to_isolate(self):
        params = assembly_params.from_dict({"assembler": "spades"})
        assert isinstance(params, assembly_params.SpadesParams)
        assert params.mode == "isolate"

    def test_accepts_the_three_declared_modes(self):
        for mode in ("isolate", "careful", "standard"):
            params = assembly_params.from_dict(
                {"assembler": "spades", "mode": mode}
            )
            assert params.mode == mode

    def test_rejects_an_unknown_mode(self):
        with pytest.raises(ValidationError):
            assembly_params.from_dict({"assembler": "spades", "mode": "meta"})

    def test_rejects_frugal_which_is_deliberately_not_offered(self):
        """--frugal's own manual says it changes results unpredictably."""
        with pytest.raises(ValidationError):
            assembly_params.from_dict({"assembler": "spades", "mode": "frugal"})

    def test_round_trips_through_as_dict(self):
        params = assembly_params.from_dict(
            {"assembler": "spades", "mode": "careful", "threads": 12}
        )
        restored = assembly_params.from_dict(params.as_dict())
        assert restored == params
