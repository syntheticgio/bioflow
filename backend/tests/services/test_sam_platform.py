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


class TestTablesAgreeWithTheEnum:
    def test_every_pattern_maps_to_a_spec_value(self):
        """The table's right-hand column is what lands in a BAM header, so a
        value outside the spec is not a style problem -- GATK warns on it and
        some tools read the platform as unknown.

        This test is why the enum exists. It caught `BGI`, which the table
        emitted for DNBSEQ/MGISEQ/BGISEQ instrument models; the spec's name
        for that platform has been DNBSEQ since April 2020 and BGI was never
        valid.
        """
        from app.services.pipeline_service import _SAM_PLATFORM_PATTERNS

        valid = {p.value for p in SamPlatform}
        offenders = [
            value for _needles, value in _SAM_PLATFORM_PATTERNS if value not in valid
        ]
        assert offenders == []

    def test_every_preset_key_is_a_spec_value(self):
        """_PLATFORM_PRESETS is keyed by SAM PL value. A key outside the
        vocabulary can never be looked up, so the preset would silently never
        apply and the platform would quietly take the short-read default.
        """
        from app.services.pipeline_service import _PLATFORM_PRESETS

        valid = {p.value for p in SamPlatform}
        assert [key for key in _PLATFORM_PRESETS if key not in valid] == []


class TestDnbseqRegression:
    def test_dnbseq_instrument_model_maps_to_the_spec_value(self):
        """Was BGI, which is not in the SAM vocabulary. A real MGI file's
        metadata.platform holds an instrument model like "DNBSEQ-T7", so this
        is the value that actually reaches a BAM header.
        """
        from app.services.pipeline_service import sam_platform

        assert sam_platform("DNBSEQ-T7") == SamPlatform.DNBSEQ
        assert sam_platform("MGISEQ-2000") == SamPlatform.DNBSEQ
        assert sam_platform("BGISEQ-500") == SamPlatform.DNBSEQ
