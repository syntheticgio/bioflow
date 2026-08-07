"""What goes into the variant-call summary prompt.

Mirrors test_de_summary_prompt.py: aggregate VcfStatsFacts are always safe to
restate, and the top-N-by-severity list is where a wrong gene/position
attribution would do the most damage.
"""

from app.services.variant_summary_prompt import build_variant_user_prompt


def _vcf_stats_facts(**overrides) -> dict:
    base = {
        "vcf_stats_summary": {
            "variants": 48213,
            "ti_tv_ratio": 2.14,
            "mean_qual": 812.3,
            "mean_depth": 34.2,
        },
        "filters": ["PASS", "LowQual"],
        "sample_count": 1,
        "reference_count": 24,
        "consequence_counts": {
            "missense_variant": 1203,
            "synonymous_variant": 980,
            "stop_gained": 4,
        },
    }
    base.update(overrides)
    return base


def _top_variants(n: int = 3) -> list[dict]:
    return [
        {"gene": "BRCA1", "position": "chr17:43094464", "consequence": "stop_gained"},
        {"gene": "TTN", "position": "chr2:178525989", "consequence": "frameshift_variant"},
        {"gene": None, "position": "chr7:1200000", "consequence": "missense_variant"},
    ][:n]


class TestAggregateFacts:
    def test_the_ti_tv_ratio_is_present(self):
        prompt = build_variant_user_prompt(
            facts=_vcf_stats_facts(), top_variants=_top_variants()
        )
        assert "2.14" in prompt

    def test_the_consequence_breakdown_is_present(self):
        prompt = build_variant_user_prompt(
            facts=_vcf_stats_facts(), top_variants=_top_variants()
        )
        assert "missense_variant" in prompt
        assert "1,203" in prompt or "1203" in prompt

    def test_too_few_variants_to_characterize_returns_none(self):
        facts = _vcf_stats_facts(
            vcf_stats_summary={"variants": 0, "ti_tv_ratio": None}
        )
        prompt = build_variant_user_prompt(facts=facts, top_variants=[])
        assert prompt is None


class TestTopVariants:
    def test_named_variants_carry_their_position_and_consequence(self):
        prompt = build_variant_user_prompt(
            facts=_vcf_stats_facts(), top_variants=_top_variants()
        )
        assert "BRCA1" in prompt
        assert "stop_gained" in prompt

    def test_an_unannotated_variant_gets_a_generic_descriptor_not_omission(self):
        prompt = build_variant_user_prompt(
            facts=_vcf_stats_facts(), top_variants=_top_variants()
        )
        assert "intergenic" in prompt.lower()


class TestSystemPrompt:
    def test_a_system_prompt_exists_and_forbids_pathogenicity_claims(self):
        from app.services.variant_summary_prompt import VARIANT_SYSTEM_PROMPT

        assert "pathogenic" in VARIANT_SYSTEM_PROMPT.lower()
