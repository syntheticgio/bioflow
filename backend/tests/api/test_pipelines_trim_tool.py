"""API surface for tool-aware trim requests."""

import pytest
from app.api.v1.pipelines import router
from app.errors import register_exception_handlers
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    return TestClient(app)


class TestTrimDefaultsTool:
    def test_defaults_for_fastp_is_the_bare_endpoint(self, client):
        resp = client.get("/pipelines/defaults")
        assert resp.status_code == 200
        assert "quality_threshold" in resp.json()["params"]

    def test_defaults_for_cutadapt(self, client):
        resp = client.get("/pipelines/defaults?tool=cutadapt")
        assert resp.status_code == 200
        assert "quality_cutoff" in resp.json()["params"]

    def test_defaults_for_trimmomatic(self, client):
        resp = client.get("/pipelines/defaults?tool=trimmomatic")
        assert resp.status_code == 200
        assert "sliding_window_size" in resp.json()["params"]

    def test_defaults_for_unknown_tool_is_a_client_error(self, client):
        resp = client.get("/pipelines/defaults?tool=not-a-tool")
        assert resp.status_code == 422
