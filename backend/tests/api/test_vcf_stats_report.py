"""Serving the Variant Results database: pagination, filtering, traversal.

Structured like test_bam_stats_reports.py -- exercised through the real route
rather than a reimplementation of its path handling. Unlike the BAM report,
the variant route reads a SQLite database rather than slicing a TSV, so the
fixture builds one with the real `build_variant_db` rather than hand-writing
rows -- that way the test also catches a schema drift between the builder and
the query route.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import pipelines as pipelines_api
from app.api.v1.pipelines import router
from app.config import settings
from app.errors import register_exception_handlers
from app.pipelines.variant_db import build_variant_db
from tests.api.bare_app import override_owner, stub_get_object

OBJECT_ID = "507f1f77bcf86cd799439011"
OTHER_ID = "507f191e810c19729de860ea"

# CHROM POS REF ALT QUAL FILTER DP [GT] -- matches vcf_stats_runner.QUERY_FORMAT,
# what build_variant_db actually parses.
VARIANT_LINES = [
    "chr1\t100\tA\tG\t50.0\tPASS\t30\t0/1",
    "chr1\t200\tC\tT\t10.0\tq10\t25\t1/1",
    "chr1\t300\tG\tGA\t80.0\tPASS\t40\t0/1",
    "chr2\t150\tT\tC\t99.0\tPASS\t35\t1/1",
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "bioinfo_home", tmp_path)

    db_path = tmp_path / "vcf_stats" / OBJECT_ID / "variants.db"
    build_variant_db(rows=iter(VARIANT_LINES), db_path=db_path)

    tsv_path = tmp_path / "vcf_stats" / OBJECT_ID / "variants.tsv"
    tsv_path.write_text("\n".join(VARIANT_LINES) + "\n")

    (tmp_path / "secret.txt").write_text("blob bytes")

    # Both ids resolve: these tests are about pagination, filtering and path
    # containment, and a stub that refused OTHER_ID would make the traversal
    # cases pass on a 404 from the lookup instead of from the check under test.
    # Cross-profile refusal is covered in test_pipelines_profiles.py.
    stub_get_object(monkeypatch, pipelines_api, known={OBJECT_ID, OTHER_ID})

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    override_owner(app)
    return TestClient(app)


def get_variants(client, object_id: str = OBJECT_ID, **params):
    return client.get(
        f"/pipelines/vcfstats/variants/{object_id}",
        params=params,
        follow_redirects=False,
    )


def get_report(client, path: str, object_id: str = OBJECT_ID):
    return client.get(
        f"/pipelines/vcfstats/report/{object_id}/{path}",
        follow_redirects=False,
    )


class TestPagination:
    def test_a_page_is_returned_with_a_correct_total(self, client):
        r = get_variants(client)
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 4
        assert len(body["rows"]) == 4
        assert body["rows"][0]["chrom"] == "chr1"
        assert body["rows"][0]["pos"] == 100

    def test_offset_pages_correctly(self, client):
        r = get_variants(client, limit=1, offset=1)
        body = r.json()
        assert len(body["rows"]) == 1
        assert body["rows"][0]["pos"] == 200
        assert body["total"] == 4


class TestFiltering:
    def test_a_filter_narrows_both_rows_and_total(self, client):
        r = get_variants(client, filter_value="PASS")
        body = r.json()
        assert body["total"] == 3
        assert all(row["filter"] == "PASS" for row in body["rows"])

    def test_contig_filter(self, client):
        r = get_variants(client, contig="chr2")
        body = r.json()
        assert body["total"] == 1
        assert body["rows"][0]["chrom"] == "chr2"


class TestSkipCount:
    def test_skip_count_returns_total_null(self, client):
        r = get_variants(client, skip_count=True)
        body = r.json()
        assert body["total"] is None
        assert len(body["rows"]) == 4


class TestMissingDatabase:
    def test_a_missing_database_is_a_404_not_a_500(self, client):
        r = get_variants(client, object_id=OTHER_ID)
        assert r.status_code == 404


class TestPathTraversal:
    @pytest.mark.parametrize(
        "attack",
        [
            "../../secret.txt",
            "../../../etc/passwd",
            "/etc/passwd",
        ],
    )
    def test_traversal_out_of_the_report_tree_is_rejected(self, client, attack):
        r = get_report(client, attack)
        assert r.status_code == 404
        assert "blob bytes" not in r.text
        assert "root:" not in r.text
