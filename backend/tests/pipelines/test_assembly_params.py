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

    def test_accepts_the_four_declared_modes(self):
        for mode in ("isolate", "careful", "meta", "standard"):
            params = assembly_params.from_dict(
                {"assembler": "spades", "mode": mode}
            )
            assert params.mode == mode

    def test_rejects_an_unknown_mode(self):
        with pytest.raises(ValidationError):
            assembly_params.from_dict({"assembler": "spades", "mode": "metaviral"})

    def test_valid_modes_come_from_the_registry(self):
        """Not a second hand-maintained list beside the registry's choices.

        Two structures spelling the same set drift apart silently, and the
        drift surfaces as a mode the dialog offers and validation rejects.
        """
        from app.pipelines import assembler_registry

        for mode in assembler_registry.modes_for(Assembler.SPADES):
            assert (
                assembly_params.from_dict(
                    {"assembler": "spades", "mode": mode}
                ).mode
                == mode
            )

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


class TestMegahitParams:
    def test_dispatches_on_the_assembler_key(self):
        params = assembly_params.from_dict({"assembler": "megahit"})
        assert isinstance(params, assembly_params.MegahitParams)
        assert params.assembler is Assembler.MEGAHIT

    def test_defaults_match_megahits_own(self):
        params = assembly_params.from_dict({"assembler": "megahit"})
        assert params.min_contig_len == 200

    def test_min_contig_len_is_bounded(self):
        for bad in (0, -1, 10_001):
            with pytest.raises(ValidationError):
                assembly_params.from_dict(
                    {"assembler": "megahit", "min_contig_len": bad}
                )

    def test_has_no_mode_field(self):
        """MEGAHIT has no isolate/meta switch, so unlike SpadesParams there
        is nothing for a mode to name."""
        params = assembly_params.from_dict({"assembler": "megahit"})
        assert not hasattr(params, "mode")
        assert "mode" not in params.as_dict()

    def test_has_no_always_true_meta_field(self):
        """Deliberate absence, not an omission.

        An always-true `meta` would sit in a run's recorded provenance with
        no command-line flag behind it -- the same lie assembly_runner's
        docstring refuses for genome size. `_is_meta_assembly` answers the
        question from the type instead.
        """
        params = assembly_params.from_dict({"assembler": "megahit"})
        assert "meta" not in params.as_dict()

    def test_accepts_genome_size_without_using_it(self):
        """The shared field stays available and stays inert: MEGAHIT's memory
        model has no genome term to feed."""
        params = assembly_params.from_dict(
            {"assembler": "megahit", "genome_size": "4.6m"}
        )
        assert params.genome_size == 4_600_000

    def test_round_trips_through_as_dict(self):
        params = assembly_params.from_dict(
            {"assembler": "megahit", "min_contig_len": 500, "threads": 12}
        )
        restored = assembly_params.from_dict(params.as_dict())
        assert restored == params


class TestIsMetaAssembly:
    """One question, three spellings -- see pipeline_service._is_meta_assembly.

    This is what decides which memory model guards the launch, so a wrong
    answer is an unguarded run rather than a cosmetic slip.
    """

    def _is_meta(self, data):
        from app.services.pipeline_service import _is_meta_assembly

        return _is_meta_assembly(assembly_params.from_dict(data))

    def test_flye_reads_the_boolean(self):
        assert self._is_meta({"assembler": "flye", "meta": True}) is True
        assert self._is_meta({"assembler": "flye", "meta": False}) is False

    def test_spades_reads_the_mode(self):
        assert self._is_meta({"assembler": "spades", "mode": "meta"}) is True
        assert self._is_meta({"assembler": "spades", "mode": "isolate"}) is False

    def test_megahit_is_always_meta(self):
        """From the type, not from a field -- MegahitParams deliberately has
        neither a `meta` boolean nor a `mode` to read."""
        assert self._is_meta({"assembler": "megahit"}) is True
        assert self._is_meta({"assembler": "megahit", "min_contig_len": 500}) is True

    def test_abyss_is_never_meta(self):
        assert self._is_meta({"assembler": "abyss"}) is False
