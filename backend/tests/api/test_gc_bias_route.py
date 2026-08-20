"""API surface for the GC-bias launch route.

Structured like `test_feature_coverage_reports.py`'s `TestLaunchFeatureCoverage`:
a bare app with no database, patching `pipeline_service.launch_gc_bias` the
way that file patches `launch_feature_coverage`, since a real `Job` needs no
database to construct. No report route exists for this stage -- the bias
curve lives directly in `ObjectDetail.facts`, so this file covers the launch
route only.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.pipelines import router
from app.errors import register_exception_handlers
from app.models import Job, JobClass, JobState
from app.services import pipeline_service
from tests.api.bare_app import override_owner

OBJECT_ID = "507f1f77bcf86cd799439011"


@pytest.fixture
def client(beanie_models):
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    override_owner(app)
    return TestClient(app)


class TestLaunchGcBias:
    def _fake_job(self) -> Job:
        return Job(
            type="gc_bias",
            owner="test-owner",
            job_class=JobClass.COMPUTE,
            state=JobState.PENDING,
            payload={"bam_id": OBJECT_ID},
        )

    def test_launches_and_returns_a_job(self, client, monkeypatch):
        captured = {}

        async def fake_launch(*, bam_id, owner, resource_override=False):
            captured["bam_id"] = str(bam_id)
            captured["owner"] = owner
            return self._fake_job()

        monkeypatch.setattr(pipeline_service, "launch_gc_bias", fake_launch)

        res = client.post("/pipelines/gc-bias", json={"bam_id": OBJECT_ID})
        assert res.status_code == 201
        body = res.json()
        assert body["type"] == "gc_bias"
        assert captured["bam_id"] == OBJECT_ID
