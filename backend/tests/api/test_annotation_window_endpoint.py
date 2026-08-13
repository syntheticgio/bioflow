"""The track viewer's window route.

Structured like test_annotation_stats_endpoints.py: the real route on a bare
FastAPI app, with object_service.get_object stubbed and a real features.db
built in a temp directory, so builder/route schema drift is caught here too.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import pipelines as pipelines_api
from app.api.v1.pipelines import router
from app.config import settings
from app.errors import register_exception_handlers
from app.pipelines.annotation_db import build_annotation_db
from app.pipelines.annotation_parse import Feature
from tests.api.bare_app import override_owner, stub_get_object

OBJECT_ID = "507f1f77bcf86cd799439011"
MISSING_ID = "507f191e810c19729de860ea"


def _f(start, end, feature_id, ftype="gene", parent=None):
    return Feature(
        contig="chr1", start=start, end=end, type=ftype, strand="+", score=None,
        name=feature_id, feature_id=feature_id,
        parents=(parent,) if parent else (),
        biotype=None, attributes=f"ID={feature_id}",
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "bioinfo_home", tmp_path)
    rows = [_f(1000 * i, 1000 * i + 500, f"g{i}") for i in range(1, 6)]
    rows.append(_f(1000, 1200, "e1", ftype="exon", parent="g1"))
    build_annotation_db(
        rows=iter(rows),
        db_path=tmp_path / "annotation_stats" / OBJECT_ID / "features.db",
    )
    stub_get_object(monkeypatch, pipelines_api, known={OBJECT_ID, MISSING_ID})

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    override_owner(app)
    return TestClient(app)


def get_window(client, object_id=OBJECT_ID, **params):
    params.setdefault("contig", "chr1")
    return client.get(
        f"/pipelines/annotationstats/window/{object_id}", params=params
    )


class TestModeSwitching:
    def test_sparse_window_returns_features(self, client):
        body = get_window(client, start=0, end=10_000).json()
        assert body["mode"] == "features"
        assert [f["feature_id"] for f in body["features"]] == [
            "g1", "g2", "g3", "g4", "g5"
        ]

    def test_children_ride_along(self, client):
        body = get_window(client, start=0, end=10_000).json()
        g1 = body["features"][0]
        assert [c["feature_id"] for c in g1["children"]] == ["e1"]

    def test_dense_window_returns_bins(self, client, monkeypatch):
        monkeypatch.setattr(pipelines_api, "ANNOTATION_DENSITY_THRESHOLD", 3)
        body = get_window(client, start=0, end=10_000, bins=10).json()
        assert body["mode"] == "binned"
        assert len(body["counts"]) == 10
        assert sum(body["counts"]) == 5

    def test_response_echoes_the_requested_window(self, client):
        body = get_window(client, start=100, end=9_000).json()
        assert (body["contig"], body["start"], body["end"]) == ("chr1", 100, 9_000)


class TestValidation:
    def test_bins_are_clamped_to_1000(self, client, monkeypatch):
        monkeypatch.setattr(pipelines_api, "ANNOTATION_DENSITY_THRESHOLD", 1)
        body = get_window(client, start=0, end=10_000_000, bins=999_999).json()
        assert len(body["counts"]) == 1000

    def test_end_before_start_is_rejected(self, client):
        assert get_window(client, start=500, end=100).status_code == 422

    def test_missing_database_404s(self, client):
        assert get_window(client, object_id=MISSING_ID, start=0, end=10).status_code == 404
