"""Tests for the SAM @RG PL vocabulary.

The values here are owned by the SAM specification, not by this repo:
https://github.com/samtools/hts-specs -- SAMv1.tex, the @RG PL row.
"""

from app.services.pipeline_service import SamPlatform


class TestSamPlatformVocabulary:
    def test_enum_is_exactly_the_sam_specs_twelve_values(self):
        """Pinned verbatim against SAMv1.tex's @RG PL row, which reads:

            Valid values: CAPILLARY, DNBSEQ (MGI/BGI), ELEMENT, HELICOS,
            ILLUMINA, IONTORRENT, LS454, ONT (Oxford Nanopore), PACBIO
            (Pacific Biosciences), SINGULAR, SOLID, and ULTIMA.

        Pins the enum's own content rather than only its consumers' agreement
        with it: a test that just checked the pattern table against the enum
        would pass while both silently lost the same member.
        """
        assert {p.value for p in SamPlatform} == {
            "CAPILLARY",
            "DNBSEQ",
            "ELEMENT",
            "HELICOS",
            "ILLUMINA",
            "IONTORRENT",
            "LS454",
            "ONT",
            "PACBIO",
            "SINGULAR",
            "SOLID",
            "ULTIMA",
        }

    def test_other_is_not_a_member(self):
        """OTHER is not in the SAM vocabulary. sam_platform() used to return
        it for unrecognized input, and the docstring claimed it was valid.
        The spec's remedy for an unrecognized technology is to omit PL
        entirely, so there is no member to fall back to -- making the invalid
        value unrepresentable is the point of this enum.
        """
        assert not hasattr(SamPlatform, "OTHER")
        assert "OTHER" not in {p.value for p in SamPlatform}
