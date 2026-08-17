"""API surface for the data source catalog."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.system import router
from app.errors import register_exception_handlers


@pytest.fixture
def client():
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    return TestClient(app)


class TestSourcesEndpoint:
    def test_returns_the_catalog(self, client):
        resp = client.get("/system/sources")
        assert resp.status_code == 200
        body = resp.json()
        assert "sources" in body
        assert len(body["sources"]) >= 3

    def test_entries_carry_what_the_page_renders(self, client):
        body = client.get("/system/sources").json()
        first = body["sources"][0]
        for field in ("name", "kind", "summary", "usage", "homepage"):
            assert first[field], f"{field} is empty"
