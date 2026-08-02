"""Which compleasm lineage to score an assembly against, from organism
metadata rather than autolineage."""

import pytest

from app.pipelines.lineage_inference import infer_lineage, is_specific


class TestInferLineage:
    def test_known_genus_gets_a_specific_override(self):
        assert infer_lineage("Saccharomyces cerevisiae S288C") == "saccharomycetaceae"

    def test_known_prokaryote_genus_with_no_dedicated_lineage_falls_back_to_domain(self):
        """escherichia has no compleasm lineage of its own (verified against
        the real remote listing), so this deliberately resolves through the
        enterobacterales override rather than the bare bacteria domain."""
        assert infer_lineage("Escherichia coli K-12") == "enterobacterales"

    def test_unrecognised_eukaryote_falls_back_to_domain(self):
        assert infer_lineage("Homo sapiens") == "eukaryota"

    def test_unrecognised_genus_defaults_to_eukaryote_domain(self):
        """Same asymmetry organism_taxonomy.is_eukaryotic documents: an
        unrecognised genus is assumed eukaryotic rather than prokaryotic,
        since scoring a eukaryotic assembly against the bacteria lineage is
        the more silently-wrong direction (every real gene reports as
        missing rather than the run failing outright)."""
        assert infer_lineage("Wobblia lunata") == "eukaryota"

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_missing_organism_returns_none(self, value):
        """None, not a guessed domain: an uploaded assembly with no organism
        metadata is a normal case, and the dialog should ask rather than
        silently score against a domain that might be wrong -- a eukaryotic
        assembly scored as bacteria reports every real gene as missing."""
        assert infer_lineage(value) is None

    def test_matching_is_case_insensitive_on_the_genus(self):
        assert infer_lineage("saccharomyces cerevisiae") == "saccharomycetaceae"

    def test_lineage_names_are_never_odb_suffixed(self):
        """compleasm's own download_lineage rewrites any suffix present to
        match --odb, so a name here that looked version-specific would be
        misleading about what will actually be downloaded."""
        for organism in ("Saccharomyces cerevisiae", "Escherichia coli", "Homo sapiens"):
            lineage = infer_lineage(organism)
            assert lineage is not None
            assert "odb" not in lineage


class TestIsSpecific:
    def test_genus_override_is_specific(self):
        assert is_specific("saccharomycetaceae") is True

    def test_domain_fallback_is_not_specific(self):
        assert is_specific("eukaryota") is False
        assert is_specific("bacteria") is False
