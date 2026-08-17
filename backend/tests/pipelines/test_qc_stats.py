"""Chemistry inference from QC numbers already on hand.

HiFi and CLR are both PACBIO_SMRT in SRA and both PACBIO in SAM, so platform
alone cannot tell them apart -- accuracy can, and NanoPlot already reports
mean_qual and mean_read_length for free. These thresholds are deliberately
conservative: an ambiguous read gets UNKNOWN and the caller falls back to the
platform default rather than presenting a guess as fact.
"""

import pytest

from app.pipelines import qc_stats
from app.pipelines.align_runner import ReadChemistry
from app.pipelines.qc_stats import infer_chemistry


class TestPacBio:
    def test_high_accuracy_long_reads_are_hifi(self):
        chemistry, reason = infer_chemistry(
            platform="PACBIO", mean_read_length=15200, mean_quality=31
        )
        assert chemistry is ReadChemistry.HIFI
        assert "15.2 kb" in reason or "15200" in reason
        assert "31" in reason

    def test_the_q20_boundary_is_inclusive_of_hifi(self):
        chemistry, _ = infer_chemistry(
            platform="PACBIO", mean_read_length=12000, mean_quality=20
        )
        assert chemistry is ReadChemistry.HIFI

    def test_low_accuracy_long_reads_are_clr(self):
        chemistry, reason = infer_chemistry(
            platform="PACBIO", mean_read_length=18000, mean_quality=10
        )
        assert chemistry is ReadChemistry.CLR
        assert reason

    def test_the_q15_boundary_is_exclusive_of_clr(self):
        """Q15 exactly is in the ambiguous band, not CLR -- CLR requires
        strictly below 15."""
        chemistry, _ = infer_chemistry(
            platform="PACBIO", mean_read_length=18000, mean_quality=15
        )
        assert chemistry is not ReadChemistry.CLR

    @pytest.mark.parametrize("quality", [15, 17, 19])
    def test_the_ambiguous_band_is_unknown_not_a_guess(self, quality):
        """Between Q15 and Q20 neither HiFi nor CLR is a safe call, so the
        answer is UNKNOWN and the caller falls back to the platform
        default."""
        chemistry, reason = infer_chemistry(
            platform="PACBIO", mean_read_length=18000, mean_quality=quality
        )
        assert chemistry is ReadChemistry.UNKNOWN
        assert reason


class TestOxfordNanopore:
    def test_high_accuracy_reads_are_duplex(self):
        chemistry, reason = infer_chemistry(
            platform="ONT", mean_read_length=8000, mean_quality=24
        )
        assert chemistry is ReadChemistry.ONT_DUPLEX
        assert reason

    def test_the_q20_boundary_is_inclusive_of_duplex(self):
        chemistry, _ = infer_chemistry(
            platform="ONT", mean_read_length=8000, mean_quality=20
        )
        assert chemistry is ReadChemistry.ONT_DUPLEX

    def test_ordinary_accuracy_reads_are_simplex(self):
        chemistry, reason = infer_chemistry(
            platform="ONT", mean_read_length=6000, mean_quality=12
        )
        assert chemistry is ReadChemistry.ONT_SIMPLEX
        assert reason

    def test_just_under_the_boundary_is_simplex(self):
        chemistry, _ = infer_chemistry(
            platform="ONT", mean_read_length=6000, mean_quality=19.9
        )
        assert chemistry is ReadChemistry.ONT_SIMPLEX


class TestMislabelledShortReads:
    """A file whose mean length is short-read-sized is mislabelled regardless
    of what platform it claims, so length overrides the platform branch."""

    def test_short_mean_length_wins_over_pacbio(self):
        chemistry, reason = infer_chemistry(
            platform="PACBIO", mean_read_length=150, mean_quality=35
        )
        assert chemistry is ReadChemistry.SHORT
        assert reason

    def test_short_mean_length_wins_over_ont(self):
        chemistry, _ = infer_chemistry(
            platform="ONT", mean_read_length=300, mean_quality=25
        )
        assert chemistry is ReadChemistry.SHORT

    def test_the_1000bp_boundary_is_exclusive_of_short(self):
        """Exactly 1000 is a real long read, not a mislabelled short one."""
        chemistry, _ = infer_chemistry(
            platform="ONT", mean_read_length=1000, mean_quality=25
        )
        assert chemistry is not ReadChemistry.SHORT


class TestMissingOrUnrecognizedInputs:
    def test_missing_quality_is_unknown_not_a_guess(self):
        chemistry, reason = infer_chemistry(
            platform="PACBIO", mean_read_length=15000, mean_quality=None
        )
        assert chemistry is ReadChemistry.UNKNOWN
        assert reason

    def test_missing_length_is_unknown_not_a_guess(self):
        chemistry, reason = infer_chemistry(
            platform="PACBIO", mean_read_length=None, mean_quality=30
        )
        assert chemistry is ReadChemistry.UNKNOWN
        assert reason

    def test_an_unrecognized_platform_is_unknown(self):
        chemistry, reason = infer_chemistry(
            platform="ILLUMINA", mean_read_length=15000, mean_quality=30
        )
        assert chemistry is ReadChemistry.UNKNOWN
        assert reason

    def test_every_branch_returns_a_non_empty_reason(self):
        """The dialog needs to say *why* it picked something, not just
        what."""
        for platform, length, quality in [
            ("PACBIO", 15000, 30),
            ("PACBIO", 18000, 10),
            ("PACBIO", 18000, 17),
            ("ONT", 8000, 24),
            ("ONT", 6000, 12),
            ("PACBIO", 150, 35),
            ("PACBIO", 15000, None),
            ("OTHER", 15000, 30),
        ]:
            _, reason = infer_chemistry(
                platform=platform, mean_read_length=length, mean_quality=quality
            )
            assert reason and reason.strip()


class TestPlatformVocabulary:
    """LONG_READ_PLATFORMS used to be an SRA-tag frozenset written out three
    times under three names (pipeline_handlers.py, reference_assembly.py,
    pipeline_service.py's _LONG_READ_QC_PLATFORMS), and _SAM_TO_SRA_PLATFORM
    in pipeline_service.py was an independently maintained inverse of this
    module's translation. Adding a long-read platform meant editing five
    places by hand with no error if you missed four. These lock the
    consumers to this module's copy so that class of drift fails a test
    instead of shipping silently.
    """

    def test_long_read_platforms_names_the_two_sra_tags(self):
        """Pins the dict's own content, not just its consumers' agreement
        with it -- a coverage test that only checks cross-file agreement
        would pass even if this dict itself silently lost a platform, since
        every consumer derives from it and would shrink in lockstep."""
        assert qc_stats.LONG_READ_PLATFORMS == {
            "OXFORD_NANOPORE": "ONT",
            "PACBIO_SMRT": "PACBIO",
        }

    def test_every_long_read_platform_has_a_chemistry_translation(self):
        """Every key qc_stats.LONG_READ_PLATFORMS defines must resolve to a
        value infer_chemistry actually recognizes -- the fall-through bug
        this dict replaced passed an unmapped platform straight through and
        infer_chemistry silently read that as UNKNOWN with a plausible
        reason rather than surfacing the miss."""
        for sra_tag, short_name in qc_stats.LONG_READ_PLATFORMS.items():
            chemistry, reason = infer_chemistry(
                platform=short_name, mean_read_length=15000, mean_quality=25
            )
            assert chemistry is not ReadChemistry.UNKNOWN, sra_tag
            assert reason

    def test_short_to_sra_is_the_exact_inverse(self):
        """Derived, not hand-written, so the two directions cannot drift
        relative to each other the way _SAM_TO_SRA_PLATFORM once could."""
        assert qc_stats.SHORT_TO_SRA_PLATFORM == {
            v: k for k, v in qc_stats.LONG_READ_PLATFORMS.items()
        }

    def test_pipeline_handlers_uses_this_modules_platform_set(self):
        from app.queue import pipeline_handlers

        assert pipeline_handlers.LONG_READ_PLATFORMS == frozenset(
            qc_stats.LONG_READ_PLATFORMS
        )

    def test_reference_assembly_uses_this_modules_platform_sets(self):
        from app.services import reference_assembly

        assert reference_assembly.LONG_READ_PLATFORMS == frozenset(
            qc_stats.LONG_READ_PLATFORMS
        )
        assert reference_assembly.SHORT_READ_PLATFORMS == qc_stats.SHORT_READ_PLATFORMS

    def test_pipeline_service_uses_this_modules_platform_sets(self):
        from app.services import pipeline_service

        assert pipeline_service._LONG_READ_QC_PLATFORMS == frozenset(
            qc_stats.LONG_READ_PLATFORMS
        )
        assert pipeline_service._SAM_TO_SRA_PLATFORM == qc_stats.SHORT_TO_SRA_PLATFORM

    def test_long_and_short_read_platforms_are_disjoint(self):
        """A platform cannot be both -- overlap would make is_short_read's
        platform-first branch order ambiguous."""
        assert not (
            set(qc_stats.LONG_READ_PLATFORMS) & qc_stats.SHORT_READ_PLATFORMS
        )
