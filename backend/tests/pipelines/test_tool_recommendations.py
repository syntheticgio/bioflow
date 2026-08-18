"""The tool-recommendation badge matrix, pinned as a contract.

`ToolMeta.recommendations` is what the frontend's tool picker renders as the
"Recommended" badge, and -- more consequentially -- what it *auto-selects*
when a dialog opens (`PipelineToolSelector.tsx` picks the first tool whose
recommendation for the reads' bucket is "recommended"). Until this module
existed, nothing tested any of it: a typo'd bucket key, a flipped level, or a
deleted entry changed which tool a user's trim or QC run defaults to, with
every other test staying green.

Two kinds of test, deliberately separate:

- Structural: bucket keys and level values must come from the vocabulary the
  frontend matches on. These hold for any future entry.
- Golden: the exact current matrix, spelled out. Changing a recommendation is
  legitimate -- but it should fail a test and be re-pinned on purpose, not
  ride along silently inside an unrelated edit to TOOL_META.
"""

from app.pipelines.tools import TOOL_META, RecommendationLevel, tool_with_meta

# The coarse read-type buckets. `chemistryBucket` in
# frontend/src/components/PipelineToolSelector.tsx produces exactly these two
# strings and looks them up verbatim in `recommendations`, so a key outside
# this set is unreachable dead data -- rendered nowhere, matched by nothing.
VALID_BUCKETS = {"short", "long"}


class TestStructure:
    def test_every_bucket_key_is_one_the_frontend_can_produce(self):
        for name, meta in TOOL_META.items():
            for bucket in meta.recommendations:
                assert bucket in VALID_BUCKETS, (
                    f"{name} recommends for bucket {bucket!r}, which "
                    "chemistryBucket() in PipelineToolSelector.tsx never "
                    "produces -- the badge would silently never render"
                )

    def test_every_level_is_a_recommendation_level(self):
        levels = {level.value for level in RecommendationLevel}
        for name, meta in TOOL_META.items():
            for bucket, level in meta.recommendations.items():
                assert level in levels, (
                    f"{name}[{bucket}] is {level!r}, not a RecommendationLevel "
                    "value -- the frontend compares against the string "
                    '"recommended" and would treat this as no badge'
                )

    def test_levels_are_stored_as_strings_not_enum_members(self):
        """The dict is serialized straight into the API response by
        `tool_with_meta`'s asdict. StrEnum members do survive JSON encoding,
        but the registry convention is `.value` (every current entry uses
        it), and mixing the two makes equality checks in tests ambiguous."""
        for name, meta in TOOL_META.items():
            for bucket, level in meta.recommendations.items():
                assert type(level) is str, f"{name}[{bucket}] should store the .value string"


class TestGoldenMatrix:
    """The exact recommendation matrix, one assertion per tool.

    If one of these fails because a recommendation genuinely changed, update
    the expected value here in the same commit -- that is the point: the
    change becomes visible in a diff of this file rather than invisible
    inside tools.py's 1400-line registry.
    """

    def test_the_full_matrix_matches(self):
        expected = {
            "fastp": {"short": "recommended", "long": "compatible"},
            "cutadapt": {"short": "compatible", "long": "recommended"},
            "trimmomatic": {"short": "compatible"},
            "fastqc": {"short": "recommended"},
            "nanoplot": {"long": "recommended"},
        }
        actual = {
            name: dict(meta.recommendations)
            for name, meta in TOOL_META.items()
            if meta.recommendations
        }
        assert actual == expected, (
            "The recommendation badge matrix changed. If intentional, re-pin "
            "it here so the change is reviewable on its own."
        )

    def test_no_aligner_carries_a_badge_today(self):
        """The align picker's default comes from
        `pipeline_service.default_align_params` (host arch + read chemistry),
        not from this matrix -- no aligner sets `recommendations`, so the
        align dialog renders no badge and auto-selects nothing through this
        path. If a badge is ever added to an aligner, this test is the
        reminder that two mechanisms would then both claim to pick the
        default, and they can disagree."""
        for name, meta in TOOL_META.items():
            kinds = {p.value for p in meta.pipelines}
            if "align" in kinds:
                assert not meta.recommendations, (
                    f"{name} now carries a recommendation badge; reconcile it "
                    "with default_align_params before shipping both"
                )


class TestPerPipelineBuckets:
    """What the auto-select actually does with the matrix, per picker."""

    def _tools_for(self, pipeline: str) -> dict:
        return {
            name: meta
            for name, meta in TOOL_META.items()
            if any(p.value == pipeline for p in meta.pipelines)
        }

    def test_trim_has_exactly_one_recommendation_per_bucket(self):
        """One badge per bucket in the trim picker: auto-select takes the
        *first* recommended tool in registry order, so a second RECOMMENDED
        in the same bucket makes the default depend on dict ordering."""
        trim = self._tools_for("trim")
        for bucket in VALID_BUCKETS:
            recommended = [
                name
                for name, meta in trim.items()
                if meta.recommendations.get(bucket) == RecommendationLevel.RECOMMENDED.value
            ]
            assert len(recommended) == 1, (
                f"trim/{bucket} should have exactly one RECOMMENDED tool, "
                f"got {recommended}"
            )

    def test_qc_has_at_least_one_recommendation_per_bucket(self):
        """QC currently carries two RECOMMENDED for short reads (fastp and
        fastqc), so exact-one cannot be asserted -- but zero would mean the
        QC dialog silently loses its default for that read type."""
        qc = self._tools_for("qc")
        for bucket in VALID_BUCKETS:
            recommended = [
                name
                for name, meta in qc.items()
                if meta.recommendations.get(bucket) == RecommendationLevel.RECOMMENDED.value
            ]
            assert recommended, f"qc/{bucket} has no RECOMMENDED tool left"


class TestApiPassthrough:
    def test_tool_with_meta_carries_recommendations(self):
        """The badge data must survive the boundary serializer: the frontend
        reads `tool.recommendations[bucket]` off the API response."""
        from app.pipelines import tools as tools_mod

        tool = tools_mod.Tool(name="fastp", path="/usr/bin/fastp", version="1")
        payload = tool_with_meta(tool)
        assert payload["recommendations"] == {"short": "recommended", "long": "compatible"}

    def test_a_tool_without_meta_still_has_the_key(self):
        """The fallback shape must stay in lockstep with ToolMeta's fields:
        the frontend indexes `recommendations` unconditionally, so a missing
        key is a TypeError in the picker, not a missing badge."""
        from app.pipelines import tools as tools_mod

        tool = tools_mod.Tool(name="not-a-real-tool", path=None, version=None)
        payload = tool_with_meta(tool)
        assert payload["recommendations"] == {}
