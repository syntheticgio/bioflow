"""What goes into the DE summary prompt.

Same discipline as test_summary_prompt.py: assert the model is given exact
numbers to restate, and that a missing gene symbol is described rather than
silently dropped.
"""

from app.services.de_summary_prompt import build_de_user_prompt


def _de_facts(**overrides) -> dict:
    base = {
        "contrast_test": "treated",
        "contrast_reference": "control",
        "alpha": 0.05,
        "samples": 6,
        "samples_by_condition": {"treated": 3, "control": 3},
        "genes_in_matrix": 18000,
        "genes_tested": 15200,
        "significant_genes": 231,
        "significant_up": 142,
        "significant_down": 89,
        "pydeseq2_version": "0.4.10",
    }
    base.update(overrides)
    return base


def _top_genes(n: int = 3) -> list[dict]:
    return [
        {"gene": "TP53", "log2fc": -2.31, "padj": 1.2e-8},
        {"gene": "MYC", "log2fc": 3.05, "padj": 4.5e-7},
        {"gene": None, "log2fc": 1.11, "padj": 2.0e-4},
    ][:n]


class TestAggregateFacts:
    def test_the_significant_gene_counts_are_present(self):
        prompt = build_de_user_prompt(facts=_de_facts(), top_genes=_top_genes())
        assert "142" in prompt
        assert "89" in prompt

    def test_the_contrast_is_named(self):
        prompt = build_de_user_prompt(facts=_de_facts(), top_genes=_top_genes())
        assert "treated" in prompt
        assert "control" in prompt

    def test_no_significant_genes_and_no_top_genes_returns_none(self):
        """Nothing worth narrating -- asking anyway invites invented findings."""
        facts = _de_facts(significant_genes=0, significant_up=0, significant_down=0)
        prompt = build_de_user_prompt(facts=facts, top_genes=[])
        assert prompt is None


class TestTopGenes:
    def test_named_genes_carry_their_log2fc_and_padj(self):
        prompt = build_de_user_prompt(facts=_de_facts(), top_genes=_top_genes())
        assert "TP53" in prompt
        assert "-2.31" in prompt

    def test_a_gene_with_no_symbol_gets_a_generic_descriptor_not_omission(self):
        prompt = build_de_user_prompt(facts=_de_facts(), top_genes=_top_genes())
        assert "unnamed transcript" in prompt.lower()


class TestSystemPrompt:
    def test_a_system_prompt_exists_and_forbids_recommendations(self):
        from app.services.de_summary_prompt import DE_SYSTEM_PROMPT

        assert "recommend" in DE_SYSTEM_PROMPT.lower()
