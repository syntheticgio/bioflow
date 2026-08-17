"""PL omission across every aligner's read-group shape.

ReadGroup builds the PL field in three separate methods because each aligner
family accepts read groups differently. That is hand-maintained parallel
structure: fixing two of the three leaves STAR writing `PL:None` into a real
BAM header while bwa is correct, and nothing fails. These tests cover all
three deliberately.
"""

import pytest

from app.pipelines.align_runner import ReadGroup


def _rg(platform):
    return ReadGroup(sample="S1", library="L1", platform=platform)


class TestPlIsOmittedWhenAbsent:
    def test_sam_header_omits_pl(self):
        header = _rg(None).as_sam_header()
        assert "PL:" not in header
        assert "PL:None" not in header
        assert header == "@RG\\tID:S1\\tSM:S1\\tLB:L1"

    def test_rg_args_omits_pl(self):
        args = _rg(None).as_rg_args()
        assert not any(a.startswith("PL:") for a in args)
        assert args == ["--rg-id", "S1", "--rg", "SM:S1", "--rg", "LB:L1"]

    def test_star_rg_fields_omits_pl(self):
        fields = _rg(None).as_star_rg_fields()
        assert not any(f.startswith("PL:") for f in fields)
        assert fields == ["ID:S1", "SM:S1", "LB:L1"]

    @pytest.mark.parametrize(
        "method", ["as_sam_header", "as_rg_args", "as_star_rg_fields"]
    )
    def test_no_shape_ever_stringifies_none(self, method):
        """The failure this whole task guards against: `PL:None` is a
        syntactically valid header field carrying a garbage value, so it
        corrupts silently rather than erroring.
        """
        rendered = str(getattr(_rg(None), method)())
        assert "None" not in rendered


class TestPlIsPresentWhenKnown:
    def test_sam_header_includes_pl(self):
        assert _rg("ILLUMINA").as_sam_header() == "@RG\\tID:S1\\tSM:S1\\tLB:L1\\tPL:ILLUMINA"

    def test_rg_args_include_pl(self):
        assert _rg("ILLUMINA").as_rg_args() == [
            "--rg-id", "S1", "--rg", "SM:S1", "--rg", "LB:L1", "--rg", "PL:ILLUMINA",
        ]

    def test_star_rg_fields_include_pl(self):
        assert _rg("ILLUMINA").as_star_rg_fields() == [
            "ID:S1", "SM:S1", "LB:L1", "PL:ILLUMINA",
        ]


class TestFromDictAcceptsAMissingPlatform:
    def test_platform_is_no_longer_required(self):
        """from_dict used to reject a falsy platform, so an unrecognized
        instrument model would have failed the whole alignment launch with
        "Read group requires platform" once sam_platform started returning
        None. Sample and library stay required.
        """
        rg = ReadGroup.from_dict({"sample": "S1", "library": "L1"})
        assert rg.platform is None

    def test_sample_and_library_are_still_required(self):
        from app.errors import ValidationError

        with pytest.raises(ValidationError):
            ReadGroup.from_dict({"library": "L1", "platform": "ILLUMINA"})
        with pytest.raises(ValidationError):
            ReadGroup.from_dict({"sample": "S1", "platform": "ILLUMINA"})
