"""API surface for the feature coverage launch and report endpoints.

Structured like test_bam_stats_reports.py and test_tile_matrix_route.py: a
bare app with no database for the report route (filesystem only), plus a
launch test that patches `pipeline_service.launch_feature_coverage` the way
test_tool_install_api.py patches its own service call, since a real `Job`
needs no database to construct.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import pipelines as pipelines_api
from app.api.v1.pipelines import router
from app.config import settings
from app.errors import register_exception_handlers
from app.models import Job, JobClass, JobState
from app.services import pipeline_service
from tests.api.bare_app import override_owner, stub_get_object

OBJECT_ID = "507f1f77bcf86cd799439011"
UNKNOWN_ID = "5f1f1f1f1f1f1f1f1f1f1f1f"

REPORT_PAYLOAD = {
    "features": [{"id": "gene1", "mean_depth": 12.5, "covered_pct": 88.0}],
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "bioinfo_home", tmp_path)

    reports = tmp_path / "feature_coverage"
    (reports / OBJECT_ID).mkdir(parents=True)
    (reports / OBJECT_ID / "coverage.json").write_text(json.dumps(REPORT_PAYLOAD))

    stub_get_object(monkeypatch, pipelines_api, known={OBJECT_ID})

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    override_owner(app)
    return TestClient(app)


class TestGetFeatureCoverageReport:
    def test_returns_the_parsed_report(self, client):
        res = client.get(f"/pipelines/feature-coverage/{OBJECT_ID}/report")
        assert res.status_code == 200
        assert res.json() == REPORT_PAYLOAD

    def test_missing_report_is_a_404(self, client, tmp_path):
        (tmp_path / "feature_coverage" / OBJECT_ID / "coverage.json").unlink()
        res = client.get(f"/pipelines/feature-coverage/{OBJECT_ID}/report")
        assert res.status_code == 404

    def test_unknown_object_is_a_404(self, client):
        res = client.get(f"/pipelines/feature-coverage/{UNKNOWN_ID}/report")
        assert res.status_code == 404


class TestLaunchFeatureCoverage:
    def _fake_job(self) -> Job:
        return Job(
            type="feature_coverage",
            owner="test-owner",
            job_class=JobClass.COMPUTE,
            state=JobState.PENDING,
            payload={"bam_id": OBJECT_ID},
        )

    def test_launches_and_returns_a_job(self, client, monkeypatch):
        captured = {}

        async def fake_launch(*, bam_id, owner, annotation_id=None):
            captured["bam_id"] = str(bam_id)
            captured["annotation_id"] = annotation_id
            captured["owner"] = owner
            return self._fake_job()

        monkeypatch.setattr(pipeline_service, "launch_feature_coverage", fake_launch)

        res = client.post("/pipelines/feature-coverage", json={"bam_id": OBJECT_ID})
        assert res.status_code == 201
        body = res.json()
        assert body["type"] == "feature_coverage"
        assert captured["bam_id"] == OBJECT_ID
        assert captured["annotation_id"] is None

    def test_passes_an_explicit_annotation_id_through(self, client, monkeypatch):
        annotation_id = "507f191e810c19729de860ea"
        captured = {}

        async def fake_launch(*, bam_id, owner, annotation_id=None):
            captured["annotation_id"] = str(annotation_id) if annotation_id else None
            return self._fake_job()

        monkeypatch.setattr(pipeline_service, "launch_feature_coverage", fake_launch)

        res = client.post(
            "/pipelines/feature-coverage",
            json={"bam_id": OBJECT_ID, "annotation_id": annotation_id},
        )
        assert res.status_code == 201
        assert captured["annotation_id"] == annotation_id
