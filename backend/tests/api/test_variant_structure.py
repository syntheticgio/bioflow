"""Serving a variant's protein structure.

Exercised through the real route, like test_vcf_stats_report.py, and against a
database built by the real `build_variant_db` so a schema drift between the
builder and this route is caught here rather than in the browser.

The route's own job is small -- walk the VCF to its organism, read the gene's
highest residue out of SQLite, and hand both to the resolver. What it must not
do is take either from the caller: `max_aa_pos` is a correctness input to the
resolver's length guard, and a client-supplied one would let a stale or
hand-edited request select a protein the variant does not belong to.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import pipelines as pipelines_api
from app.api.v1.pipelines import router
from app.config import settings
from app.errors import register_exception_handlers
from app.pipelines.variant_db import build_variant_db
from app.services import structure_lookup
from tests.api.bare_app import override_owner, stub_get_object

OBJECT_ID = "507f1f77bcf86cd799439011"
MISSING_ID = "507f191e810c19729de860ea"

YEAST_TAXID = 559292

# CHROM POS REF ALT QUAL FILTER BCSQ DP GT -- BCSQ sits *ahead* of DP, per
# vcf_stats_runner.QUERY_FORMAT, so that its position does not shift with the
# sample count. Getting this order wrong parses every annotation column as
# null while still inserting the row, which is a silent enough failure to be
# worth spelling out here.
ANNOTATED_LINES = [
    "chr1\t100\tA\tG\t50.0\tPASS\tmissense|PKC1|YBL105C|protein_coding|+|866I>866L|1000G>C\t30\t0/1",
    "chr1\t200\tC\tT\t60.0\tPASS\tmissense|PKC1|YBL105C|protein_coding|+|729T>729A|900A>G\t30\t0/1",
    "chr1\t300\tG\tA\t70.0\tPASS\tsynonymous|ADH1|YOL086C|protein_coding|+|100P\t30\t0/1",
    "chr1\t400\tT\tC\t80.0\tPASS\t.\t30\t0/1",
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "bioinfo_home", tmp_path)
    build_variant_db(
        rows=iter(ANNOTATED_LINES),
        db_path=tmp_path / "vcf_stats" / OBJECT_ID / "variants.db",
    )

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    override_owner(app)
    return TestClient(app)


@pytest.fixture
def resolver(monkeypatch):
    """Record what the route asks the resolver for.

    The arguments are the assertion in most of these tests: whether a
    structure comes back is UniProt's business, but *what was asked* is
    entirely this route's.
    """
    calls = []

    async def _resolve(*, gene, taxid, max_aa_pos):
        calls.append({"gene": gene, "taxid": taxid, "max_aa_pos": max_aa_pos})
        return _resolve.hit

    _resolve.hit = structure_lookup.StructureHit(
        accession="P24583", pdb_ids=["1ABC", "2DEF"], length=1151
    )
    _resolve.calls = calls
    monkeypatch.setattr(pipelines_api.structure_lookup, "resolve_structure", _resolve)
    return _resolve


@pytest.fixture
def taxid(monkeypatch):
    """Stand in for the VCF -> reference -> tax_id walk, which needs Mongo."""
    async def _taxid(vcf):
        return _taxid.value

    _taxid.value = YEAST_TAXID
    monkeypatch.setattr(pipelines_api.pipeline_service, "taxid_for_vcf", _taxid)
    return _taxid


@pytest.fixture
def vcf_object(monkeypatch):
    """A VCF object exists for OBJECT_ID, and only for it.

    MISSING_ID deliberately stays unknown so the stub raises `NotFoundError`
    for it, exactly as the real owner-scoped lookup does. That is what keeps
    TestGuards' 404 cases honest -- a stub that resolved every id would turn
    them into assertions about the SQLite path instead.
    """
    return stub_get_object(monkeypatch, pipelines_api, known={OBJECT_ID})


def get_structure(client, gene="PKC1", object_id=OBJECT_ID):
    return client.get(
        f"/pipelines/vcfstats/structure/{object_id}",
        params={"gene": gene},
        follow_redirects=False,
    )


class TestResolution:
    def test_returns_the_resolved_structure(self, client, resolver, taxid, vcf_object):
        r = get_structure(client)
        assert r.status_code == 200
        body = r.json()
        assert body["accession"] == "P24583"
        assert body["pdb_ids"] == ["1ABC", "2DEF"]

    def test_max_aa_pos_comes_from_the_database(
        self, client, resolver, taxid, vcf_object
    ):
        """The highest residue the gene carries, read from SQLite.

        PKC1's rows are 866 and 729, so 866 is what the length guard has to be
        given. Taking this from the client would let a request select a
        protein too short for a residue actually present in the callset.
        """
        get_structure(client, gene="PKC1")
        assert resolver.calls[0]["max_aa_pos"] == 866

    def test_the_taxid_is_passed_through(self, client, resolver, taxid, vcf_object):
        get_structure(client)
        assert resolver.calls[0]["taxid"] == YEAST_TAXID

    def test_a_gene_with_no_structure_is_a_normal_answer(
        self, client, resolver, taxid, vcf_object
    ):
        """65% of resolved genes have none. This is a 200 with an empty list,
        not an error -- the UI says "no structure available" and that is an
        ordinary thing for it to say."""
        resolver.hit = structure_lookup.StructureHit(
            accession="P12685", pdb_ids=[], length=1235
        )
        r = get_structure(client)
        assert r.status_code == 200
        assert r.json()["pdb_ids"] == []
        assert r.json()["accession"] == "P12685"

    def test_an_unresolvable_gene_is_a_normal_answer(
        self, client, resolver, taxid, vcf_object
    ):
        """Also not an error. A symbol UniProt cannot place, and a UniProt
        outage, reach the UI as the same sentence."""
        resolver.hit = None
        r = get_structure(client)
        assert r.status_code == 200
        assert r.json()["accession"] is None
        assert r.json()["pdb_ids"] == []


class TestGuards:
    def test_a_gene_absent_from_the_callset_does_not_query(
        self, client, resolver, taxid, vcf_object
    ):
        """No residue means no length guard, so there is nothing to ask
        UniProt that could be validated."""
        r = get_structure(client, gene="NOTINTHISVCF")
        assert r.status_code == 200
        assert r.json()["accession"] is None
        assert resolver.calls == []

    def test_a_gene_with_no_residue_does_not_query(
        self, client, resolver, taxid, vcf_object
    ):
        """ADH1 here is synonymous-only.

        A synonymous variant has an aa_pos but changes no residue, so a
        structure view of it would show an unchanged protein. The button is
        gated in the UI; this is the server refusing the same thing.
        """
        r = get_structure(client, gene="ADH1")
        assert r.status_code == 200
        assert r.json()["accession"] is None
        assert resolver.calls == []

    def test_a_missing_taxid_does_not_query(
        self, client, resolver, taxid, vcf_object
    ):
        """An unscoped gene lookup is a wrong answer, not a broad one."""
        taxid.value = None
        r = get_structure(client)
        assert r.status_code == 200
        assert r.json()["accession"] is None
        assert resolver.calls == []

    def test_an_unknown_object_is_404(self, client, resolver, taxid, vcf_object):
        vcf_object.value = None
        assert get_structure(client, object_id=MISSING_ID).status_code == 404

    def test_a_vcf_without_computed_results_is_404(
        self, client, resolver, taxid, vcf_object
    ):
        """No variants.db means results were never computed -- the same
        condition the variants route reports."""
        assert get_structure(client, object_id=MISSING_ID).status_code == 404
