"""The annotation subset export routes.

Structured like test_annotation_stats_endpoints.py: exercised through the
real router on a bare FastAPI app with `object_service.get_object` stubbed
and `settings.bioinfo_home` pointed at a temp directory, rather than standing
up Mongo or the queue. These tests cover the 404 (no computed results) and
validation (bad output_name) branches -- the actual enqueue path is
exercised by the queue/handler tests, not here.
"""

import pytest
from app.api.v1 import pipelines as pipelines_api
from app.api.v1.pipelines import router
from app.config import settings
from app.errors import register_exception_handlers
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.api.bare_app import override_owner, stub_get_object

OBJECT_ID = "507f1f77bcf86cd799439011"
OTHER_ID = "507f191e810c19729de860ea"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "bioinfo_home", tmp_path)

    # No features.db is built for either id here -- these tests are about the
    # "no computed results" 404 and the output_name validation, neither of
    # which needs a real index on disk.
    stub_get_object(monkeypatch, pipelines_api, known={OBJECT_ID, OTHER_ID})

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    override_owner(app)
    return TestClient(app)


def get_export_count(client, object_id: str = OBJECT_ID, **params):
    return client.get(
        f"/pipelines/annotationstats/export-count/{object_id}",
        params=params,
        follow_redirects=False,
    )


def post_export(client, **body):
    return client.post(
        "/pipelines/annotationstats/export",
        json=body,
        follow_redirects=False,
    )


class TestExportCountRoute:
    def test_404_when_not_computed(self, client):
        r = get_export_count(client)
        assert r.status_code == 404
        assert "Compute results first" in r.json()["message"]

    def test_accepts_start_min_and_start_max(self, client):
        # No features.db either way, so this still 404s -- the point is that
        # start_min/start_max pass FastAPI's query-param validation and reach
        # the same "no computed results" branch as a request without them,
        # proving the route declares them rather than rejecting them as
        # unknown params.
        r = get_export_count(client, contig="chr1", start_min=1000, start_max=2000)
        assert r.status_code == 404
        assert "Compute results first" in r.json()["message"]


class TestExportRoute:
    def test_404_when_not_computed(self, client):
        r = post_export(client, object_id=OBJECT_ID)
        assert r.status_code == 404
        assert "Compute results first" in r.json()["message"]

    def test_rejects_output_name_with_path_separator(self, client):
        r = post_export(client, object_id=OBJECT_ID, output_name="foo/bar.gff3")
        assert r.status_code == 422

    def test_rejects_output_name_with_dot_dot(self, client):
        r = post_export(client, object_id=OBJECT_ID, output_name="../etc/passwd")
        assert r.status_code == 422

    def test_rejects_output_name_of_dot(self, client):
        # "." has no "/" and no ".." substring, so it slips past a naive
        # separator/dot-dot check -- but it resolves to the scratch directory
        # itself inside the handler, crashing the job with IsADirectoryError
        # instead of failing cleanly here at the API boundary.
        r = post_export(client, object_id=OBJECT_ID, output_name=".")
        assert r.status_code == 422

    def test_allows_a_normal_output_name(self, client):
        # Passes the validation check and reaches the (here, still-404)
        # "no computed results" branch -- proving a plain filename isn't
        # rejected by the path-traversal guard.
        r = post_export(client, object_id=OBJECT_ID, output_name="chr1.subset.gff3")
        assert r.status_code == 404
        assert "Compute results first" in r.json()["message"]

    def test_accepts_start_min_and_start_max(self, client):
        # Same reasoning as the export-count case: no features.db, so this
        # still 404s, but start_min/start_max must pass Pydantic body
        # validation on AnnotationExportRequest to reach that branch at all.
        r = post_export(
            client,
            object_id=OBJECT_ID,
            contig="chr1",
            start_min=1000,
            start_max=2000,
        )
        assert r.status_code == 404
        assert "Compute results first" in r.json()["message"]
