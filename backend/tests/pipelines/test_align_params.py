"""Per-aligner parameter validation.

The property worth testing is that a knob belonging to one tool cannot be set
on another. A silently ignored parameter is the failure mode that matters
here: the run completes, the recorded provenance says one thing, and the
command that actually ran said another.
"""

import pytest

from app.errors import ValidationError
from app.pipelines import align_params
from app.pipelines.aligners import Aligner


class TestDispatch:
    def test_from_dict_returns_the_class_for_the_named_aligner(self):
        p = align_params.from_dict({"aligner": "bowtie2"})
        assert isinstance(p, align_params.Bowtie2Params)

    def test_hisat2_gets_its_own_class(self):
        p = align_params.from_dict({"aligner": "hisat2"})
        assert isinstance(p, align_params.Hisat2Params)

    def test_minimap2_still_carries_its_preset(self):
        p = align_params.from_dict({"aligner": "minimap2", "preset": "map-ont"})
        assert p.preset == "map-ont"

    def test_an_unknown_aligner_is_rejected(self):
        with pytest.raises(ValueError):
            align_params.from_dict({"aligner": "not-a-real-aligner"})


class TestSharedValidation:
    def test_threads_must_be_at_least_one(self):
        with pytest.raises(ValidationError):
            align_params.from_dict({"aligner": "bowtie2", "threads": 0})

    def test_sort_memory_has_a_floor(self):
        """Below this samtools spills to disk, which is slower than the
        memory saved is worth."""
        with pytest.raises(ValidationError):
            align_params.from_dict({"aligner": "bowtie2", "sort_memory_mb": 32})


class TestEnsureWired:
    """`ensure_wired` is the stopgap for `AlignParams.from_dict` (the
    Minimap2Params alias) not dispatching on the `aligner` key. Until Task 7
    rewires the two call sites onto `align_params.from_dict`, this is what
    stops a bowtie2/HISAT2 request from being silently aligned with
    minimap2 instead.
    """

    def test_bwa_mem2_is_wired(self):
        align_params.ensure_wired(Aligner.BWA_MEM2.value)  # does not raise

    def test_minimap2_is_wired(self):
        align_params.ensure_wired(Aligner.MINIMAP2.value)  # does not raise

    def test_bowtie2_is_rejected(self):
        with pytest.raises(ValidationError, match="not wired"):
            align_params.ensure_wired(Aligner.BOWTIE2.value)

    def test_hisat2_is_rejected(self):
        with pytest.raises(ValidationError, match="not wired"):
            align_params.ensure_wired(Aligner.HISAT2.value)

    def test_missing_aligner_is_not_this_guards_job(self):
        """No `aligner` key means `AlignParams.from_dict` will default to
        minimap2, which is correct behavior -- not this guard's concern."""
        align_params.ensure_wired(None)  # does not raise

    def test_an_unknown_aligner_name_is_not_this_guards_job(self):
        """A name that is not a real `Aligner` member at all is
        `from_dict`'s error to raise (a clear ValueError), not this guard's
        -- it only rejects names that are valid but unwired."""
        align_params.ensure_wired("not-a-real-aligner")  # does not raise

    def test_a_future_fifth_aligner_is_unwired_by_default(self):
        """UNWIRED_ALIGNERS is derived as everything minus the two known-
        wired aligners, not a hardcoded pair -- so an aligner added to the
        enum before Task 7 lands stays rejected without editing this
        guard."""
        assert align_params.UNWIRED_ALIGNERS == frozenset(Aligner) - {
            Aligner.BWA_MEM2,
            Aligner.MINIMAP2,
        }


class TestBowtie2:
    def test_sensitivity_defaults_to_sensitive(self):
        p = align_params.from_dict({"aligner": "bowtie2"})
        assert p.sensitivity == "--sensitive"

    def test_an_unknown_sensitivity_is_rejected(self):
        with pytest.raises(ValidationError):
            align_params.from_dict(
                {"aligner": "bowtie2", "sensitivity": "--extremely-sensitive"}
            )

    def test_maxins_is_carried(self):
        p = align_params.from_dict({"aligner": "bowtie2", "maxins": 800})
        assert p.maxins == 800

    def test_maxins_must_be_positive(self):
        with pytest.raises(ValidationError):
            align_params.from_dict({"aligner": "bowtie2", "maxins": 0})


class TestHisat2:
    def test_rna_strandness_defaults_to_unstranded(self):
        p = align_params.from_dict({"aligner": "hisat2"})
        assert p.rna_strandness == ""

    def test_rna_strandness_accepts_the_documented_values(self):
        for value in ("FR", "RF", "F", "R", ""):
            p = align_params.from_dict(
                {"aligner": "hisat2", "rna_strandness": value}
            )
            assert p.rna_strandness == value

    def test_an_unknown_strandness_is_rejected(self):
        """A wrong value here silently breaks downstream strand-specific
        counting rather than failing, so it must not reach the command."""
        with pytest.raises(ValidationError):
            align_params.from_dict({"aligner": "hisat2", "rna_strandness": "XY"})


class TestRoundTrip:
    def test_as_dict_round_trips_through_from_dict(self):
        """Params are persisted on the run record and read back when a run is
        inspected, so the two directions have to agree."""
        original = align_params.from_dict(
            {"aligner": "bowtie2", "threads": 8, "maxins": 700, "local": True}
        )
        restored = align_params.from_dict(original.as_dict())
        assert restored == original
