"""The resolve endpoint's dispatch.

One field, four input classes. The test is that each input reaches the right
branch -- a gene symbol must not be sent as an accession, and a species-level
taxon must produce a picker rather than "nothing found".
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import uniprot as uniprot_router
from app.errors import register_exception_handlers
from app.metadata import uniprot as uniprot_meta


@pytest.fixture
def client():
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(uniprot_router.router)
    return TestClient(app)


@pytest.fixture
def stub(monkeypatch):
    """Stub every outward call the router can make."""
    calls = {"taxon": [], "name": [], "proteome": [], "proteins": [], "counts": []}

    def fake_resolve_taxon(taxon_id):
        calls["taxon"].append(taxon_id)
        return uniprot_meta.TaxonResolution(
            proteome=uniprot_meta.ProteomeInfo(
                id="UP000002311",
                name="Saccharomyces cerevisiae",
                taxon_id=559292,
                strain="S288c",
                protein_count=6067,
                is_reference=True,
                busco_score=99,
                genome_assembly="GCA_000146045.2",
            ),
            candidates=[],
            needs_picker=False,
        )

    def fake_resolve_organism_name(name):
        calls["name"].append(name)
        return uniprot_meta.TaxonResolution()

    def fake_resolve_proteome(pid):
        calls["proteome"].append(pid)
        return uniprot_meta.ProteomeInfo(
            id=pid,
            name="Saccharomyces cerevisiae",
            taxon_id=559292,
            strain=None,
            protein_count=6067,
            is_reference=True,
            busco_score=99,
            genome_assembly="GCA_000146045.2",
        )

    def fake_search_proteins(query):
        calls["proteins"].append(query)
        return [
            uniprot_meta.ProteinHit(
                accession="P0DTC2",
                entry_id="SPIKE_SARS2",
                name="Spike glycoprotein",
                organism="SARS-CoV-2",
                length=1273,
                reviewed=True,
            )
        ]

    def fake_count(query, **kwargs):
        calls["counts"].append(query)
        return 20416 if "reviewed" in query else 147506

    monkeypatch.setattr(uniprot_meta, "resolve_taxon", fake_resolve_taxon)
    monkeypatch.setattr(uniprot_meta, "resolve_organism_name", fake_resolve_organism_name)
    monkeypatch.setattr(uniprot_meta, "resolve_proteome", fake_resolve_proteome)
    monkeypatch.setattr(uniprot_meta, "search_proteins", fake_search_proteins)
    monkeypatch.setattr(uniprot_meta, "count_results", fake_count)
    return calls


class TestDispatch:
    def test_a_proteome_id_returns_a_proteome(self, client, stub):
        resp = client.post("/uniprot/resolve", json={"query": "UP000002311"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "proteome"
        assert body["proteome"]["id"] == "UP000002311"
        assert stub["proteome"] == ["UP000002311"]

    def test_a_taxon_returns_a_proteome(self, client, stub):
        resp = client.post("/uniprot/resolve", json={"query": "559292"})
        assert resp.status_code == 200
        assert resp.json()["kind"] == "proteome"
        assert stub["taxon"] == [559292]

    def test_free_text_returns_proteins(self, client, stub):
        resp = client.post("/uniprot/resolve", json={"query": "spike glycoprotein"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "proteins"
        assert body["proteins"][0]["accession"] == "P0DTC2"

    def test_a_gene_symbol_reaches_the_protein_search(self, client, stub):
        """EGFR is not an accession. Sending it as one returns nothing."""
        resp = client.post("/uniprot/resolve", json={"query": "EGFR"})
        assert resp.json()["kind"] == "proteins"
        assert stub["proteins"], "should have run a protein search"

    def test_accessions_return_those_proteins(self, client, stub):
        resp = client.post("/uniprot/resolve", json={"query": "P0DTC2"})
        body = resp.json()
        assert body["kind"] == "proteins"
        assert "accession:P0DTC2" in stub["proteins"][0]

    def test_a_proteome_carries_both_counts(self, client, stub):
        """The ~7x reviewed/unreviewed split, shown at the moment of choice
        rather than discovered after downloading 147,506 entries."""
        resp = client.post("/uniprot/resolve", json={"query": "UP000002311"})
        body = resp.json()
        assert body["proteome"]["reviewed_count"] == 20416
        assert body["proteome"]["total_count"] == 147506

    def test_a_proteome_carries_its_genome_assembly(self, client, stub):
        resp = client.post("/uniprot/resolve", json={"query": "UP000002311"})
        assert resp.json()["proteome"]["genome_assembly"] == "GCA_000146045.2"

    def test_an_empty_query_is_rejected(self, client, stub):
        resp = client.post("/uniprot/resolve", json={"query": "   "})
        assert resp.status_code == 422
