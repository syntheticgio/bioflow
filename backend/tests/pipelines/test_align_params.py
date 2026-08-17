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

    def test_winnowmap_gets_its_own_class(self):
        p = align_params.from_dict({"aligner": "winnowmap"})
        assert isinstance(p, align_params.WinnowmapParams)


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


class TestStar:
    def test_unmapped_reads_are_kept_by_default(self):
        """A departure from STAR's own default, and deliberate: every number
        the alignment report shows comes from `samtools flagstat`, and
        flagstat over a BAM with the unmapped reads discarded reports 100%
        mapped whatever the truth was."""
        p = align_params.from_dict({"aligner": "star"})
        assert p.out_sam_unmapped is True

    def test_multimap_limit_defaults_to_stars_own(self):
        p = align_params.from_dict({"aligner": "star"})
        assert p.out_filter_multimap_nmax == 20

    def test_a_multimap_limit_below_one_is_rejected(self):
        """0 would make STAR discard every read, producing an empty BAM from
        a job that exited cleanly."""
        with pytest.raises(ValidationError):
            align_params.from_dict(
                {"aligner": "star", "out_filter_multimap_nmax": 0}
            )

    def test_a_negative_intron_max_is_rejected(self):
        with pytest.raises(ValidationError):
            align_params.from_dict({"aligner": "star", "align_intron_max": -1})

    def test_intron_max_defaults_to_stars_derived_ceiling(self):
        p = align_params.from_dict({"aligner": "star"})
        assert p.align_intron_max == 0


class TestWinnowmap:
    def test_preset_defaults_to_map_pb(self):
        p = align_params.from_dict({"aligner": "winnowmap"})
        assert p.preset == "map-pb"

    def test_short_read_preset_is_rejected(self):
        """winnowmap has no short-read mode -- it exists purely to
        cross-check minimap2 on long reads for GCI, so "sr" is not a valid
        choice the way it is for minimap2."""
        with pytest.raises(ValidationError):
            align_params.from_dict({"aligner": "winnowmap", "preset": "sr"})

    def test_long_read_presets_are_accepted(self):
        for preset in ("map-pb", "map-ont", "map-hifi"):
            p = align_params.from_dict({"aligner": "winnowmap", "preset": preset})
            assert p.preset == preset

    def test_k_defaults_to_gcis_readme_example(self):
        p = align_params.from_dict({"aligner": "winnowmap"})
        assert p.k == 15

    def test_k_above_28_is_rejected(self):
        """winnowmap's own -k help: "k-mer size (no larger than 28)"."""
        with pytest.raises(ValidationError):
            align_params.from_dict({"aligner": "winnowmap", "k": 29})

    def test_k_below_one_is_rejected(self):
        with pytest.raises(ValidationError):
            align_params.from_dict({"aligner": "winnowmap", "k": 0})

    def test_distinct_defaults_to_gcis_readme_example(self):
        p = align_params.from_dict({"aligner": "winnowmap"})
        assert p.distinct == 0.9998

    def test_distinct_must_be_a_fraction(self):
        with pytest.raises(ValidationError):
            align_params.from_dict({"aligner": "winnowmap", "distinct": 1.5})
        with pytest.raises(ValidationError):
            align_params.from_dict({"aligner": "winnowmap", "distinct": 0.0})


class TestRoundTrip:
    def test_as_dict_round_trips_through_from_dict(self):
        """Params are persisted on the run record and read back when a run is
        inspected, so the two directions have to agree."""
        original = align_params.from_dict(
            {"aligner": "bowtie2", "threads": 8, "maxins": 700, "local": True}
        )
        restored = align_params.from_dict(original.as_dict())
        assert restored == original

    def test_star_round_trips_too(self):
        """Including `out_sam_unmapped=False`, which is the one field whose
        default is True -- a round trip that dropped it would silently turn
        the setting back on."""
        original = align_params.from_dict(
            {
                "aligner": "star",
                "two_pass": True,
                "align_intron_max": 1,
                "out_sam_unmapped": False,
            }
        )
        restored = align_params.from_dict(original.as_dict())
        assert restored == original
        assert restored.out_sam_unmapped is False

    def test_winnowmap_round_trips_too(self):
        original = align_params.from_dict(
            {"aligner": "winnowmap", "preset": "map-hifi", "k": 21, "distinct": 0.999}
        )
        restored = align_params.from_dict(original.as_dict())
        assert restored == original
