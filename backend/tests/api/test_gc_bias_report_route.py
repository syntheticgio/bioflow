"""API surface for the gc-bias per-contig blobplot report route.

Structured like test_feature_coverage_reports.py's own report-route tests: a
bare app with no database (filesystem only), the object read exists only to
produce the 404.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import pipelines as pipelines_api
from app.api.v1.pipelines import router
from app.config import settings
from app.errors import register_exception_handlers
from tests.api.bare_app import override_owner, stub_get_object

OBJECT_ID = "507f1f77bcf86cd799439011"
UNKNOWN_ID = "5f1f1f1f1f1f1f1f1f1f1f1f"

REPORT_PAYLOAD = {
    "contigs": [{"contig": "c1", "gc": 45.0, "mean_depth": 12.5, "length": 1000}],
    "dropped_count": 0,
    "kept_count": 1,
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "bioinfo_home", tmp_path)

    reports = tmp_path / "gc_bias"
    (reports / OBJECT_ID).mkdir(parents=True)
    (reports / OBJECT_ID / "gc_blob.json").write_text(json.dumps(REPORT_PAYLOAD))

    stub_get_object(monkeypatch, pipelines_api, known={OBJECT_ID})

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    override_owner(app)
    return TestClient(app)


class TestGetGcBiasReport:
    def test_returns_the_parsed_report(self, client):
        res = client.get(f"/pipelines/gc-bias/{OBJECT_ID}/report")
        assert res.status_code == 200
        assert res.json() == REPORT_PAYLOAD

    def test_missing_report_is_a_404(self, client, tmp_path):
        (tmp_path / "gc_bias" / OBJECT_ID / "gc_blob.json").unlink()
        res = client.get(f"/pipelines/gc-bias/{OBJECT_ID}/report")
        assert res.status_code == 404

    def test_unknown_object_is_a_404(self, client):
        res = client.get(f"/pipelines/gc-bias/{UNKNOWN_ID}/report")
        assert res.status_code == 404
