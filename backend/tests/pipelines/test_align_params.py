"""Per-aligner parameter validation.

The property worth testing is that a knob belonging to one tool cannot be set
on another. A silently ignored parameter is the failure mode that matters
here: the run completes, the recorded provenance says one thing, and the
command that actually ran said another.
"""

import pytest

from app.errors import ValidationError
from app.pipelines import align_params


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
