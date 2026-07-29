"""API surface for variant calling requests."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.pipelines import VariantRequest, router
from app.errors import register_exception_handlers


@pytest.fixture
def client():
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    return TestClient(app)


class TestVariantRequestShape:
    """The request model is the contract the dialog codes against."""

    def test_bam_id_is_the_only_required_field(self):
        req = VariantRequest(bam_id="000000000000000000000001")
        assert req.reference_id is None
        assert req.caller is None
        assert req.params == {}

    def test_accepts_an_explicit_reference(self):
        """The escape hatch for an uploaded BAM, which carries no record of
        what it was aligned against."""
        req = VariantRequest(
            bam_id="000000000000000000000001",
            reference_id="000000000000000000000002",
        )
        assert req.reference_id is not None

    def test_rejects_a_malformed_id(self):
        with pytest.raises(ValueError):
            VariantRequest(bam_id="not-an-object-id")


class TestVariantEndpoints:
    """Request validation only. These endpoints reach the database as soon as
    the body parses, and this client has none -- the not-found and launch
    paths are covered at the service level in test_launch_rules.py, where the
    decisions actually live."""

    def test_launch_rejects_a_malformed_id(self, client):
        resp = client.post("/pipelines/variants", json={"bam_id": "nope"})
        assert resp.status_code == 422

    def test_launch_requires_a_bam_id(self, client):
        resp = client.post("/pipelines/variants", json={})
        assert resp.status_code == 422

    def test_launch_rejects_a_malformed_reference_id(self, client):
        resp = client.post(
            "/pipelines/variants",
            json={"bam_id": "000000000000000000000001", "reference_id": "nope"},
        )
        assert resp.status_code == 422


class TestVariantToolsAreListed:
    def test_tools_endpoint_includes_the_variant_callers(self, client):
        """The tool panel is where a user learns a caller is unavailable
        before spending a job to find out."""
        resp = client.get("/pipelines/tools")
        assert resp.status_code == 200
        names = {t["name"] for t in resp.json()["tools"]}
        assert {"clair3", "bcftools"} <= names

    def test_variant_callers_report_a_variant_pipeline(self, client):
        resp = client.get("/pipelines/tools")
        by_name = {t["name"]: t for t in resp.json()["tools"]}
        assert "variant" in by_name["clair3"]["pipelines"]
        assert "variant" in by_name["bcftools"]["pipelines"]

    def test_tools_endpoint_includes_one_liner(self, client):
        """Regression: `tool_with_meta` forwarded `summary`/`strengths`/
        `runnable` from `TOOL_META` but dropped `one_liner`, so the real API
        response omitted it even though every `TOOL_META` entry has one."""
        resp = client.get("/pipelines/tools")
        by_name = {t["name"]: t for t in resp.json()["tools"]}
        assert by_name["clair3"]["one_liner"]
        assert by_name["bcftools"]["one_liner"]
