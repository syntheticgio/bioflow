"""Organism-name search: taxon autocomplete and genome search by taxon.

No test here touches the network. `sra._get` is stubbed directly, the same
seam `ncbi_assembly.py`'s own lookups go through, so what's exercised is the
parsing and never-raises behavior, not NCBI's actual API.
"""

import json

from app.metadata import ncbi_taxonomy


class TestSuggestOrganisms:
    def test_parses_matches_from_a_real_shaped_response(self, monkeypatch):
        payload = {
            "sci_name_and_ids": [
                {
                    "sci_name": "Homo sapiens",
                    "tax_id": "9606",
                    "common_name": "human",
                    "matched_term": "Homo sapiens",
                    "rank": "SPECIES",
                    "group_name": "primates",
                },
                {
                    "sci_name": "Homo sapiens neanderthalensis",
                    "tax_id": "63221",
                    "common_name": "Neandertal",
                    "matched_term": "Homo sapiens neanderthalensis",
                    "rank": "SUBSPECIES",
                    "group_name": "primates",
                },
            ]
        }
        monkeypatch.setattr(ncbi_taxonomy, "_get", lambda url: json.dumps(payload))

        result = ncbi_taxonomy.suggest_organisms("hom")

        assert len(result) == 2
        assert result[0].sci_name == "Homo sapiens"
        assert result[0].tax_id == 9606
        assert result[0].common_name == "human"
        assert result[0].rank == "SPECIES"

    def test_empty_query_short_circuits_without_a_network_call(self, monkeypatch):
        def explode(url):
            raise AssertionError("should not be called for an empty query")

        monkeypatch.setattr(ncbi_taxonomy, "_get", explode)
        assert ncbi_taxonomy.suggest_organisms("") == []
        assert ncbi_taxonomy.suggest_organisms("   ") == []

    def test_network_failure_yields_an_empty_list(self, monkeypatch):
        monkeypatch.setattr(ncbi_taxonomy, "_get", lambda url: None)
        assert ncbi_taxonomy.suggest_organisms("hom") == []

    def test_unparseable_body_yields_an_empty_list(self, monkeypatch):
        monkeypatch.setattr(ncbi_taxonomy, "_get", lambda url: "not json")
        assert ncbi_taxonomy.suggest_organisms("hom") == []

    def test_malformed_entries_are_skipped_not_fatal(self, monkeypatch):
        payload = {
            "sci_name_and_ids": [
                {"sci_name": "Homo sapiens", "tax_id": "9606"},
                {"sci_name": "Missing tax_id"},
                {"tax_id": "123"},
                "not even a dict",
            ]
        }
        monkeypatch.setattr(ncbi_taxonomy, "_get", lambda url: json.dumps(payload))
        result = ncbi_taxonomy.suggest_organisms("hom")
        assert len(result) == 1
        assert result[0].sci_name == "Homo sapiens"

    def test_missing_sci_name_and_ids_key_yields_empty_list(self, monkeypatch):
        monkeypatch.setattr(ncbi_taxonomy, "_get", lambda url: json.dumps({}))
        assert ncbi_taxonomy.suggest_organisms("hom") == []


class TestSearchAssembliesByTaxon:
    def test_parses_a_page_of_assembly_reports(self, monkeypatch):
        payload = {
            "reports": [
                {
                    "current_accession": "GCF_000002445.2",
                    "organism": {
                        "tax_id": 185431,
                        "organism_name": "Trypanosoma brucei brucei TREU927",
                    },
                    "assembly_info": {
                        "assembly_level": "Chromosome",
                        "refseq_category": "reference genome",
                    },
                    "assembly_stats": {"total_sequence_length": "26131000"},
                },
                {
                    "current_accession": "GCA_000002445.1",
                    "organism": {
                        "tax_id": 185431,
                        "organism_name": "Trypanosoma brucei brucei TREU927",
                    },
                },
            ],
            "next_page_token": "abc123",
            "total_count": 42,
        }
        monkeypatch.setattr(ncbi_taxonomy, "_get", lambda url: json.dumps(payload))

        page = ncbi_taxonomy.search_assemblies_by_taxon(185431)

        assert len(page.assemblies) == 2
        assert page.assemblies[0].accession == "GCF_000002445.2"
        assert page.assemblies[0].total_length == 26131000
        assert page.assemblies[0].refseq_category == "reference genome"
        assert page.assemblies[1].refseq_category is None
        assert page.next_page_token == "abc123"
        assert page.total_count == 42

    def test_page_token_and_reference_only_reach_the_request(self, monkeypatch):
        seen = {}

        def fake_get(url):
            seen["url"] = url
            return json.dumps({"reports": []})

        monkeypatch.setattr(ncbi_taxonomy, "_get", fake_get)
        ncbi_taxonomy.search_assemblies_by_taxon(
            9606, page_token="next-page", page_size=5, reference_only=True
        )

        assert "page_token=next-page" in seen["url"]
        assert "page_size=5" in seen["url"]
        assert "filters.reference_only=true" in seen["url"]

    def test_assembly_level_reaches_the_request(self, monkeypatch):
        """Confirmed live against the Datasets API: `filters.assembly_level`
        narrows a taxon's dataset_report the same way `filters.reference_only`
        does (42 complete genomes vs. 1754 total assemblies for S. cerevisiae
        at tax_id 4932)."""
        seen = {}

        def fake_get(url):
            seen["url"] = url
            return json.dumps({"reports": []})

        monkeypatch.setattr(ncbi_taxonomy, "_get", fake_get)
        ncbi_taxonomy.search_assemblies_by_taxon(4932, assembly_level="complete_genome")

        assert "filters.assembly_level=complete_genome" in seen["url"]

    def test_no_assembly_level_omits_the_filter(self, monkeypatch):
        seen = {}

        def fake_get(url):
            seen["url"] = url
            return json.dumps({"reports": []})

        monkeypatch.setattr(ncbi_taxonomy, "_get", fake_get)
        ncbi_taxonomy.search_assemblies_by_taxon(4932)

        assert "assembly_level" not in seen["url"]

    def test_network_failure_yields_an_empty_page(self, monkeypatch):
        monkeypatch.setattr(ncbi_taxonomy, "_get", lambda url: None)
        page = ncbi_taxonomy.search_assemblies_by_taxon(9606)
        assert page.assemblies == []
        assert page.next_page_token is None
        assert page.total_count is None

    def test_unparseable_body_yields_an_empty_page(self, monkeypatch):
        monkeypatch.setattr(ncbi_taxonomy, "_get", lambda url: "not json")
        page = ncbi_taxonomy.search_assemblies_by_taxon(9606)
        assert page.assemblies == []

    def test_a_malformed_record_is_dropped_not_fatal(self, monkeypatch):
        payload = {
            "reports": [
                {"current_accession": "GCF_000002445.2"},
                "not even a dict",
                {},
            ]
        }
        monkeypatch.setattr(ncbi_taxonomy, "_get", lambda url: json.dumps(payload))
        page = ncbi_taxonomy.search_assemblies_by_taxon(9606)
        assert len(page.assemblies) == 1
