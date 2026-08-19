"""Serving the SV Results database: pagination, filtering, summary, 404.

Mirrors test_vcf_stats_report.py -- exercised through the real route rather
than a reimplementation of its path handling. The fixture builds the SQLite
table with the real `build_sv_db` rather than hand-writing rows, so the test
also catches a schema drift between the builder and the query routes. The db
path is built the same way the route resolves it -- sv_stats_dir/<object_id>/
sv.db, keyed by the SV VCF's own id, not the source BAM -- so a wrong
resolution would show up as a 404 here rather than only in production.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import pipelines as pipelines_api
from app.api.v1.pipelines import router
from app.config import settings
from app.errors import register_exception_handlers
from app.pipelines.sv_db import build_sv_db
from tests.api.bare_app import override_owner, stub_get_object

OBJECT_ID = "507f1f77bcf86cd799439011"
OTHER_ID = "507f191e810c19729de860ea"

# Minimal VCF data lines: CHROM POS ID REF ALT QUAL FILTER INFO FORMAT SAMPLE
SV_LINES = [
    "chr1\t1000\t.\tN\t<DEL>\t50.0\tPASS\tSVTYPE=DEL;SVLEN=-200;END=1200;SUPPORT=10\tGT\t0/1",
    "chr1\t5000\t.\tN\t<INS>\t30.0\tq5\tSVTYPE=INS;SVLEN=150;SUPPORT=4\tGT\t1/1",
    "chr2\t2000\t.\tN\t<DUP>\t80.0\tPASS\tSVTYPE=DUP;SVLEN=50000;END=52000;SUPPORT=15\tGT\t0/1",
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "bioinfo_home", tmp_path)

    db_path = tmp_path / "sv_stats" / OBJECT_ID / "sv.db"
    build_sv_db(rows=iter(SV_LINES), db_path=db_path)

    stub_get_object(monkeypatch, pipelines_api, known={OBJECT_ID, OTHER_ID})

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    override_owner(app)
    return TestClient(app)


def get_svs(client, object_id: str = OBJECT_ID, **params):
    return client.get(
        f"/pipelines/structural_variants/svs/{object_id}",
        params=params,
        follow_redirects=False,
    )


def get_summary(client, object_id: str = OBJECT_ID):
    return client.get(
        f"/pipelines/structural_variants/summary/{object_id}",
        follow_redirects=False,
    )


class TestPagination:
    def test_a_page_is_returned_with_a_correct_total(self, client):
        r = get_svs(client)
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 3
        assert len(body["rows"]) == 3
        assert body["rows"][0]["chrom"] == "chr1"
        assert body["rows"][0]["pos"] == 1000

    def test_offset_pages_correctly(self, client):
        # Ordered chrom, pos: chr1:1000, chr1:5000, chr2:2000 -- offset 1
        # lands on the second chr1 row, not chr2.
        r = get_svs(client, limit=1, offset=1)
        body = r.json()
        assert len(body["rows"]) == 1
        assert body["rows"][0]["pos"] == 5000
        assert body["total"] == 3


class TestFiltering:
    def test_a_filter_narrows_both_rows_and_total(self, client):
        r = get_svs(client, filter_value="PASS")
        body = r.json()
        assert body["total"] == 2
        assert all(row["filter"] == "PASS" for row in body["rows"])

    def test_contig_filter(self, client):
        r = get_svs(client, contig="chr2")
        body = r.json()
        assert body["total"] == 1
        assert body["rows"][0]["chrom"] == "chr2"

    def test_svtype_filter(self, client):
        r = get_svs(client, svtype="DEL")
        body = r.json()
        assert body["total"] == 1
        assert body["rows"][0]["svtype"] == "DEL"

    def test_min_length_filter(self, client):
        r = get_svs(client, min_length=1000)
        body = r.json()
        assert body["total"] == 1
        assert body["rows"][0]["svtype"] == "DUP"


class TestSkipCount:
    def test_skip_count_returns_total_null(self, client):
        r = get_svs(client, skip_count=True)
        body = r.json()
        assert body["total"] is None
        assert len(body["rows"]) == 3


class TestSummary:
    def test_type_counts_and_histogram(self, client):
        r = get_summary(client)
        assert r.status_code == 200
        body = r.json()
        assert body["type_counts"] == {"DEL": 1, "INS": 1, "DUP": 1}
        assert len(body["length_histogram"]) == 6
        assert sum(b["count"] for b in body["length_histogram"]) == 3


class TestMissingDatabase:
    def test_a_missing_database_is_a_404_not_a_500_for_svs(self, client):
        r = get_svs(client, object_id=OTHER_ID)
        assert r.status_code == 404

    def test_a_missing_database_is_a_404_not_a_500_for_summary(self, client):
        r = get_summary(client, object_id=OTHER_ID)
        assert r.status_code == 404
