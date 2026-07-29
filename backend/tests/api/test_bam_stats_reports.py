"""Serving the per-contig BAM stats report: pagination, download, traversal.

Structured like test_qc_reports.py -- exercised through the real route rather
than a reimplementation of its path handling.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.pipelines import router
from app.config import settings
from app.errors import register_exception_handlers

OBJECT_ID = "507f1f77bcf86cd799439011"
OTHER_ID = "507f191e810c19729de860ea"

CONTIGS_TSV = (
    "contig\tlength\treads\tunmapped_reads\tcovered_bases"
    "\tcoverage_pct\tmean_depth\tmean_baseq\tmean_mapq\n"
    "chr1\t1000\t500\t10\t990\t99.0\t20.0\t35.0\t55.0\n"
    "chr2\t2000\t300\t5\t1900\t95.0\t12.0\t34.0\t54.0\n"
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "bioinfo_home", tmp_path)

    reports = tmp_path / "bam_stats"
    (reports / OBJECT_ID).mkdir(parents=True)
    (reports / OBJECT_ID / "contigs.tsv").write_text(CONTIGS_TSV)
    (reports / OTHER_ID).mkdir(parents=True)
    (reports / OTHER_ID / "contigs.tsv").write_text("contig\tlength\nchrX\t500\n")

    (tmp_path / "secret.txt").write_text("blob bytes")

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    return TestClient(app)


def get(client, path: str, object_id: str = OBJECT_ID, **params):
    return client.get(
        f"/pipelines/bamstats/report/{object_id}/{path}",
        params=params,
        follow_redirects=False,
    )


class TestDownload:
    def test_download_returns_the_whole_tsv(self, client):
        r = get(client, "contigs.tsv", download=1)
        assert r.status_code == 200
        assert "chr1" in r.text
        assert "chr2" in r.text
        assert r.headers["content-type"].startswith("text/tab-separated-values")

    def test_download_content_type_is_not_sniffed(self, client):
        r = get(client, "contigs.tsv", download=1)
        assert r.headers["x-content-type-options"] == "nosniff"


class TestPagination:
    def test_default_page_returns_json_rows(self, client):
        r = get(client, "contigs.tsv")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 2
        assert body["rows"][0]["contig"] == "chr1"

    def test_limit_and_offset(self, client):
        r = get(client, "contigs.tsv", limit=1, offset=1)
        body = r.json()
        assert len(body["rows"]) == 1
        assert body["rows"][0]["contig"] == "chr2"

    def test_a_missing_report_is_a_404(self, client):
        assert get(client, "never_ran.tsv").status_code == 404

    def test_numeric_columns_are_coerced(self, client):
        r = get(client, "contigs.tsv")
        row = r.json()["rows"][0]
        assert row["length"] == 1000
        assert isinstance(row["length"], int)
        assert row["coverage_pct"] == 99.0
        assert isinstance(row["coverage_pct"], float)


class TestPathTraversal:
    @pytest.mark.parametrize(
        "attack",
        [
            "../../secret.txt",
            "../../../etc/passwd",
            "/etc/passwd",
        ],
    )
    def test_traversal_out_of_the_report_tree_serves_nothing(self, client, attack):
        r = get(client, attack)
        assert "blob bytes" not in r.text
        assert "root:" not in r.text
