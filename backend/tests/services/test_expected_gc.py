"""The expected-GC cascade.

Tier 1 (a measured project reference) has its own tests in this file once the
database seam exists; these cover the table and the resolution order.
"""

import pytest

from app.services import expected_gc


class TestGenomeTable:
    def test_every_entry_carries_a_citation(self):
        """The reason the table is allowed to exist at all. A GC percentage
        drawn as an authoritative reference curve with no source is the
        fabricated-value failure TOOL_META's required fields exist to stop."""
        for key, entry in expected_gc.GENOME_GC.items():
            assert entry.citation, f"{key} has no citation"
            assert entry.source_name, f"{key} has no source_name"

    def test_every_value_is_a_plausible_percentage(self):
        for key, entry in expected_gc.GENOME_GC.items():
            assert 0 < entry.percent < 100, f"{key} has an impossible GC"

    def test_keys_are_already_normalized(self):
        """Lookup normalizes the user's input, not the table. A table key that
        is not already normalized is unreachable, silently."""
        from app.models import normalize_organism

        for key in expected_gc.GENOME_GC:
            assert key == normalize_organism(key)


class TestFromOrganism:
    def test_resolves_a_known_organism(self):
        got = expected_gc.from_organism("Homo sapiens")
        assert got is not None
        assert got.source == "table"
        assert 40 < got.percent < 42

    def test_is_case_and_whitespace_insensitive(self):
        """'homo sapiens' and 'Homo  sapiens' are one species; normalize_organism
        is what the OrganismBlurb cache already keys on."""
        a = expected_gc.from_organism("homo  sapiens")
        b = expected_gc.from_organism("Homo sapiens")
        assert a is not None and b is not None
        assert a.percent == b.percent

    def test_an_unknown_organism_resolves_to_nothing(self):
        assert expected_gc.from_organism("Nonexistent organism") is None

    def test_blank_input_resolves_to_nothing(self):
        assert expected_gc.from_organism("") is None
        assert expected_gc.from_organism(None) is None

    def test_attribution_names_the_organism_and_the_assembly(self):
        """The curve must always be able to say where its number came from.

        `source_name` lives on the table entry, not on the resolved
        ExpectedGc -- the resolved object carries only the finished
        attribution string, so this reads the table for the expected text."""
        got = expected_gc.from_organism("Escherichia coli")
        assert got is not None
        assert "Escherichia coli" in got.attribution
        assert expected_gc.GENOME_GC["escherichia coli"].source_name in got.attribution
