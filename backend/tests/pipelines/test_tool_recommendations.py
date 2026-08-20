"""The tool-recommendation badge matrix, pinned as a contract.

`ToolMeta.recommendations` is what the frontend's tool picker renders as the
"Recommended" badge, and -- more consequentially -- what it *auto-selects*
when a dialog opens. `PipelineToolSelector.tsx` takes the recommended tool
that names the reads' bucket in `default_for`, falling back to the first
recommended one; before #588 there was no `default_for` and the fallback was
the only rule, so a bucket with two recommended tools resolved by whichever
this registry happened to list first. Until this module existed, nothing
tested any of it: a typo'd bucket key, a flipped level, or a
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
            # Added by 6e595a21, which tagged aligners by read chemistry
            # without re-pinning this matrix -- it left two tests in this
            # file red on main until #588 picked them up.
            "bwa-mem2": {"short": "recommended", "long": "incompatible"},
            "bowtie2": {"short": "compatible", "long": "incompatible"},
            "hisat2": {"short": "compatible", "long": "incompatible"},
            "star": {"short": "compatible", "long": "incompatible"},
            "minimap2": {"short": "compatible", "long": "recommended"},
            # Filtlong is COMPATIBLE for long reads in the trim picker (cutadapt
            # holds the RECOMMENDED slot there); it gets its own filter_long_reads
            # card as the sole recommended choice for long-read filtering.
            "filtlong": {"long": "compatible"},
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

    def test_aligner_badges_agree_with_default_align_params(self):
        """Two mechanisms pick the align dialog's default, and they must not
        disagree.

        This test used to assert that *no* aligner carried a badge at all.
        6e595a21 added badges to all five and did not update it, so it sat
        red on main. The premise it was protecting is still the real one --
        `pipeline_service.default_align_params` chooses by host arch and read
        chemistry, and the badge matrix now chooses too -- so it asserts the
        agreement rather than the absence: bwa-mem2 for short reads and
        minimap2 for long, exactly what default_align_params falls through
        to when both binaries are available."""
        expected_default = {"short": "bwa-mem2", "long": "minimap2"}
        for bucket, tool in expected_default.items():
            recommended = [
                name
                for name, meta in TOOL_META.items()
                if any(p.value == "align" for p in meta.pipelines)
                and meta.recommendations.get(bucket)
                == RecommendationLevel.RECOMMENDED.value
            ]
            assert recommended == [tool], (
                f"align/{bucket} recommends {recommended}, but "
                f"default_align_params picks {tool!r} -- the picker's badge "
                "and the server-side default would disagree"
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
        """QC carries two RECOMMENDED for short reads (fastp and fastqc) on
        purpose -- `run_qc` runs both as a pair -- so exact-one is the wrong
        assertion here, unlike trim. Zero would mean the QC dialog silently
        loses its default for that read type."""
        qc = self._tools_for("qc")
        for bucket in VALID_BUCKETS:
            recommended = [
                name
                for name, meta in qc.items()
                if meta.recommendations.get(bucket) == RecommendationLevel.RECOMMENDED.value
            ]
            assert recommended, f"qc/{bucket} has no RECOMMENDED tool left"

    def test_every_bucket_with_a_tie_names_exactly_one_default(self):
        """Wherever a picker family has more than one RECOMMENDED tool in a
        bucket, exactly one of them must set `default_for` for that bucket.

        Without it the frontend's auto-select takes whichever came first in
        TOOL_META's dict order, so an unrelated reordering of the registry
        silently changes which tool a dialog opens on (#588).
        """
        families = {p for meta in TOOL_META.values() for p in meta.pipelines}
        for family in families:
            members = self._tools_for(family.value)
            for bucket in VALID_BUCKETS:
                recommended = [
                    name
                    for name, meta in members.items()
                    if meta.recommendations.get(bucket)
                    == RecommendationLevel.RECOMMENDED.value
                ]
                if len(recommended) < 2:
                    continue
                defaults = [
                    name for name in recommended if bucket in TOOL_META[name].default_for
                ]
                assert len(defaults) == 1, (
                    f"{family.value}/{bucket} has {len(recommended)} RECOMMENDED "
                    f"tools ({recommended}) but {len(defaults)} marked default "
                    f"({defaults}) -- auto-select would fall back to dict order"
                )

    def test_default_for_buckets_are_ones_the_tool_recommends(self):
        """`default_for` breaks a tie between RECOMMENDED tools, so naming a
        bucket the tool does not recommend (or is incompatible with) would
        promote it over the tools that are actually recommended there."""
        for name, meta in TOOL_META.items():
            for bucket in meta.default_for:
                assert bucket in VALID_BUCKETS, (
                    f"{name} is default for bucket {bucket!r}, which "
                    "chemistryBucket() never produces"
                )
                assert (
                    meta.recommendations.get(bucket)
                    == RecommendationLevel.RECOMMENDED.value
                ), (
                    f"{name} is default for {bucket!r} without being "
                    "RECOMMENDED there"
                )

    def test_qc_short_defaults_to_fastp(self):
        """The pinned resolution of #588. fastp is the tool `run_qc` cannot
        finish without -- FastQC is the optional extra whose absence the
        handler tolerates -- so it is the honest default of the two."""
        assert "short" in TOOL_META["fastp"].default_for
        assert "short" not in TOOL_META["fastqc"].default_for


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
