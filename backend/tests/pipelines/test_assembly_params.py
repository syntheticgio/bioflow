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


def test_spades_still_refused_as_not_installed():
    """SPAdes stays declared-but-unavailable so #519 has somewhere to land."""
    with pytest.raises(ValidationError, match="not installed in this build"):
        assembly_params.from_dict({"assembler": "spades"})
