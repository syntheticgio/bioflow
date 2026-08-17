"""The registry-driven schema endpoint.

Only the schema route is covered here. The envelope route loads two objects
from Mongo, and this suite mounts the router against a bare FastAPI app with
no database -- so an envelope test would be testing the fixture, not the
endpoint. The envelope's real logic (the arithmetic and the bands) is covered
directly in test_resource_estimator.py, and the wiring is verified by hand in
Task 14.
"""

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


class TestSchemaEndpoint:
    @pytest.mark.parametrize(
        "aligner", ["bwa-mem2", "minimap2", "bowtie2", "hisat2", "star"]
    )
    def test_every_aligner_has_a_schema(self, client, aligner):
        resp = client.get(f"/pipelines/aligners/{aligner}/schema")
        assert resp.status_code == 200
        assert resp.json()["aligner"] == aligner

    def test_fields_carry_what_the_form_needs(self, client):
        resp = client.get("/pipelines/aligners/bowtie2/schema")
        fields = {f["key"]: f for f in resp.json()["fields"]}
        assert fields["maxins"]["kind"] == "int"
        assert fields["sensitivity"]["kind"] == "select"
        assert fields["sensitivity"]["choices"]
        assert fields["threads"]["group"] == "performance"

    def test_help_text_survives_serialization(self, client):
        """The generated form has no other explanation for a knob, so an
        empty help string is a field with no stated meaning."""
        resp = client.get("/pipelines/aligners/hisat2/schema")
        for f in resp.json()["fields"]:
            assert f["help"].strip()

    def test_an_unknown_aligner_is_a_client_error(self, client):
        resp = client.get("/pipelines/aligners/not-real/schema")
        assert resp.status_code == 404
