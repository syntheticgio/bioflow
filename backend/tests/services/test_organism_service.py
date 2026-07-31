"""Which metadata values are worth asking a model about, and how they key.

The organism field is free text that real records fill with placeholders, bare
tax IDs and whole pasted descriptions. Every one of those that gets through
becomes a permanent cache row with a confabulated paragraph attached, so the
guard protects the cache at least as much as it protects the model.
"""

import pytest

from app.models import normalize_organism
from app.services.organism_service import is_summarizable


class TestNormalization:
    @pytest.mark.parametrize(
        "variant",
        ["Homo sapiens", "homo sapiens", "HOMO SAPIENS", "  Homo   sapiens  "],
    )
    def test_case_and_spacing_variants_share_one_key(self, variant):
        """Otherwise one species accumulates a cache row per spelling."""
        assert normalize_organism(variant) == "homo sapiens"

    def test_strain_suffixes_are_kept_because_they_are_different_organisms(self):
        """'E. coli K-12' and 'E. coli O157:H7' deserve different paragraphs."""
        assert normalize_organism("Escherichia coli K-12") != normalize_organism(
            "Escherichia coli O157:H7"
        )


class TestAcceptedValues:
    @pytest.mark.parametrize(
        "organism",
        [
            "Homo sapiens",
            "Escherichia coli K-12",
            "Saccharomyces cerevisiae",
            # Genus alone is a real and describable answer.
            "Escherichia",
        ],
    )
    def test_real_organisms_are_summarizable(self, organism):
        assert is_summarizable(organism) is True


class TestRejectedValues:
    @pytest.mark.parametrize(
        "value",
        ["unknown", "N/A", "none", "not collected", "unspecified", "missing"],
        ids=lambda v: v.replace(" ", "-"),
    )
    def test_placeholders_that_appear_in_real_metadata_are_rejected(self, value):
        assert is_summarizable(value) is False

    @pytest.mark.parametrize(
        "value",
        ["synthetic construct", "metagenome", "uncultured"],
    )
    def test_non_species_values_are_rejected(self, value):
        """Real SRA values, but nothing a species blurb can describe."""
        assert is_summarizable(value) is False

    @pytest.mark.parametrize(
        "value",
        [None, "", "   ", "x", "12345", "---"],
        ids=["none", "empty", "spaces", "too-short", "bare-digits", "punctuation"],
    )
    def test_junk_is_rejected(self, value):
        assert is_summarizable(value) is False

    def test_a_pasted_description_is_too_long_to_be_a_species(self):
        assert is_summarizable("Homo sapiens " * 30) is False
