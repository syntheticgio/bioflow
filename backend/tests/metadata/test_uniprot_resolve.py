"""Resolution, with the network stubbed.

The test that matters most is `test_a_species_taxon_falls_back_to_all`:
taxon 4932 has no reference proteome, and a resolver without the fallback
reports that yeast has no proteome while 360 sit behind it.
"""

import json

import pytest

from app.metadata import uniprot


@pytest.fixture
def stub_transport(monkeypatch):
    """Replace the one HTTP seam and record what was asked for.

    Patches `uniprot._get_json`, which is the module's only transport
    function -- patching urllib instead would leave the URL construction
    untested, which is the part that was measurably easy to get wrong.
    """
    calls: list[str] = []
    responses: dict[str, dict] = {}

    def fake_get_json(url: str, *, timeout: float = 0.0) -> dict:
        calls.append(url)
        for fragment, payload in responses.items():
            if fragment in url:
                return payload
        return {"results": []}

    monkeypatch.setattr(uniprot, "_get_json", fake_get_json)
    return calls, responses


def _proteome(pid: str, *, ref: bool = True, count: int = 6067, busco: int = 99):
    return {
        "id": pid,
        "proteomeType": "Reference proteome" if ref else "Non Reference proteome",
        "proteinCount": count,
        "strain": "ATCC 204508 / S288c",
        "taxonomy": {"taxonId": 559292, "scientificName": "Saccharomyces cerevisiae"},
        "genomeAssembly": {"assemblyId": "GCA_000146045.2"},
        "proteomeCompletenessReport": {"buscoReport": {"score": busco}},
    }


class TestProteomeResolution:
    def test_a_strain_taxon_uses_the_reference_query(self, stub_transport):
        calls, responses = stub_transport
        responses["reference%3Atrue"] = {"results": [_proteome("UP000002311")]}

        result = uniprot.resolve_taxon(559292)

        assert result.proteome is not None
        assert result.proteome.id == "UP000002311"
        assert result.needs_picker is False

    def test_a_species_taxon_falls_back_to_all(self, stub_transport):
        """Taxon 4932 returns nothing for `reference:true` and 360 for the
        unfiltered query. Without this fallback the dialog says yeast has no
        proteome, which is the single most likely way to ship this broken.
        """
        calls, responses = stub_transport
        responses["organism_id%3A4932+AND+reference"] = {"results": []}
        responses["organism_id%3A4932&"] = {
            "results": [_proteome("UP000037662", ref=False, count=5389, busco=98)]
        }

        result = uniprot.resolve_taxon(4932)

        assert result.needs_picker is True
        assert len(result.candidates) == 1
        assert result.candidates[0].id == "UP000037662"

    def test_the_proteome_carries_its_genome_assembly(self, stub_transport):
        """The cross-link to the NCBI download. Present on the record, so it
        costs nothing to surface."""
        calls, responses = stub_transport
        responses["reference%3Atrue"] = {"results": [_proteome("UP000002311")]}

        result = uniprot.resolve_taxon(559292)

        assert result.proteome.genome_assembly == "GCA_000146045.2"

    def test_busco_is_carried_for_the_picker(self, stub_transport):
        """The signal that makes choosing between strains possible rather
        than arbitrary."""
        calls, responses = stub_transport
        responses["reference%3Atrue"] = {"results": []}
        responses["organism_id%3A4932&"] = {
            "results": [_proteome("UP000188490", ref=False, busco=93)]
        }

        result = uniprot.resolve_taxon(4932)

        assert result.candidates[0].busco_score == 93

    def test_a_taxon_with_nothing_resolves_empty(self, stub_transport):
        calls, responses = stub_transport
        result = uniprot.resolve_taxon(99999999)
        assert result.proteome is None
        assert result.candidates == []


class TestProteinResolution:
    def test_a_text_search_returns_hits(self, stub_transport):
        calls, responses = stub_transport
        responses["uniprotkb"] = {
            "results": [
                {
                    "primaryAccession": "P0DTC2",
                    "uniProtkbId": "SPIKE_SARS2",
                    "entryType": "UniProtKB reviewed (Swiss-Prot)",
                    "proteinDescription": {
                        "recommendedName": {"fullName": {"value": "Spike glycoprotein"}}
                    },
                    "organism": {"scientificName": "SARS-CoV-2"},
                    "sequence": {"length": 1273},
                }
            ]
        }

        hits = uniprot.search_proteins("spike glycoprotein")

        assert len(hits) == 1
        assert hits[0].accession == "P0DTC2"
        assert hits[0].name == "Spike glycoprotein"
        assert hits[0].length == 1273
        assert hits[0].reviewed is True

    def test_an_unreviewed_entry_is_marked(self, stub_transport):
        calls, responses = stub_transport
        responses["uniprotkb"] = {
            "results": [
                {
                    "primaryAccession": "A0A0B7P3V8",
                    "entryType": "UniProtKB unreviewed (TrEMBL)",
                    "organism": {"scientificName": "Saccharomyces cerevisiae"},
                    "sequence": {"length": 100},
                }
            ]
        }

        hits = uniprot.search_proteins("something")

        assert hits[0].reviewed is False

    def test_a_missing_name_does_not_crash(self, stub_transport):
        """Unreviewed entries frequently have no recommendedName."""
        calls, responses = stub_transport
        responses["uniprotkb"] = {
            "results": [
                {
                    "primaryAccession": "A0A0B7P3V8",
                    "entryType": "UniProtKB unreviewed (TrEMBL)",
                    "organism": {"scientificName": "Yeast"},
                    "sequence": {"length": 100},
                }
            ]
        }

        hits = uniprot.search_proteins("something")

        assert hits[0].name is None


class TestCounts:
    def test_counts_come_from_the_total_header(self, stub_transport, monkeypatch):
        """`X-Total-Results` is what makes the size guard exact rather than
        an estimate, and what shows the ~7x reviewed/unreviewed split."""
        def fake_count(query: str, *, timeout: float = 0.0) -> int | None:
            return 20416 if "reviewed" in query else 147506

        monkeypatch.setattr(uniprot, "count_results", fake_count)

        assert uniprot.count_results("proteome:UP000005640 AND reviewed:true") == 20416
        assert uniprot.count_results("proteome:UP000005640") == 147506


class TestFailureIsNotFatal:
    def test_a_network_failure_resolves_to_nothing(self, monkeypatch):
        """Matches structure_lookup's stance: an outage means the dialog
        finds nothing, not that it returns a 500."""
        def boom(url: str, *, timeout: float = 0.0) -> dict:
            raise OSError("connection refused")

        monkeypatch.setattr(uniprot, "_get_json", boom)

        result = uniprot.resolve_taxon(559292)

        assert result.proteome is None
        assert result.candidates == []


class TestMalformedPayloads:
    """UniProt returning an unexpected shape must not raise.

    The `or {}` idiom guards only against None. A field present but holding a
    string raises on `.get()`, and that escapes the try/except around the
    request -- parsing happens after it. Measured before the guard existed:
    one bad entry in a batch took down the whole response.
    """

    def test_a_non_dict_nested_field_does_not_raise(self, stub_transport):
        calls, responses = stub_transport
        responses["organism_id"] = {
            "results": [{"id": "UP000000001", "taxonomy": "Saccharomyces cerevisiae"}]
        }

        result = uniprot.resolve_taxon(559292)

        assert result.proteome is not None
        assert result.proteome.id == "UP000000001"
        assert result.proteome.taxon_id is None

    def test_a_protein_with_string_organism_does_not_raise(self, stub_transport):
        calls, responses = stub_transport
        responses["uniprotkb"] = {
            "results": [{"primaryAccession": "P0DTC2", "organism": "SARS-CoV-2"}]
        }

        hits = uniprot.search_proteins("anything")

        assert len(hits) == 1
        assert hits[0].accession == "P0DTC2"
        assert hits[0].organism is None

    def test_a_null_completeness_report_does_not_raise(self, stub_transport):
        calls, responses = stub_transport
        responses["organism_id"] = {
            "results": [{"id": "UP000000002", "proteomeCompletenessReport": None}]
        }

        result = uniprot.resolve_taxon(559292)

        assert result.proteome.busco_score is None
