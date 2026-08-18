"""The registry-driven schema endpoint.

Only the schema route is covered here. The envelope route loads two objects
from Mongo, and this suite mounts the router against a bare FastAPI app with
no database -- so an envelope test would be testing the fixture, not the
endpoint. The envelope's real logic (the arithmetic and the bands) is covered
directly in test_resource_estimator.py, and the wiring is verified by hand in
Task 14.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.pipelines import router
from app.errors import register_exception_handlers


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

    def test_minimap2_schema_includes_curated_fields(self, client):
        resp = client.get("/pipelines/aligners/minimap2/schema")
        fields = {f["key"]: f for f in resp.json()["fields"]}
        assert set(fields) >= {
            "preset",
            "kmer_size",
            "window_size",
            "min_chain_score",
            "max_gap",
            "secondary_ratio",
            "max_secondary",
            "secondary_mode",
            "batch_size",
            "soft_clip_supplementary",
            "cs_mode",
            "emit_md",
        }
        assert fields["preset"]["kind"] == "select"
        assert fields["preset"]["group"] == "biology"
        assert fields["kmer_size"]["kind"] == "int"
        assert fields["kmer_size"]["group"] == "biology"
        assert fields["kmer_size"]["min"] == 1
        assert fields["kmer_size"]["max"] == 28
        assert fields["window_size"]["kind"] == "int"
        assert fields["window_size"]["group"] == "biology"
        assert fields["window_size"]["min"] == 1
        assert fields["window_size"]["max"] == 255
        assert fields["min_chain_score"]["kind"] == "int"
        assert fields["min_chain_score"]["group"] == "biology"
        assert fields["min_chain_score"]["min"] == 1
        assert fields["max_gap"]["kind"] == "int"
        assert fields["max_gap"]["group"] == "biology"
        assert fields["max_gap"]["min"] == 1
        assert fields["secondary_ratio"]["kind"] == "float"
        assert fields["secondary_ratio"]["group"] == "biology"
        assert fields["secondary_ratio"]["min"] == 0
        assert fields["secondary_ratio"]["max"] == 1
        assert fields["max_secondary"]["kind"] == "int"
        assert fields["max_secondary"]["group"] == "biology"
        assert fields["max_secondary"]["min"] == 1
        assert fields["secondary_mode"]["kind"] == "select"
        assert fields["secondary_mode"]["group"] == "performance"
        assert [c["value"] for c in fields["secondary_mode"]["choices"]] == [
            "default",
            "enabled",
            "disabled",
        ]
        assert fields["batch_size"]["kind"] == "int"
        assert fields["batch_size"]["group"] == "performance"
        assert fields["batch_size"]["min"] == 1
        assert fields["soft_clip_supplementary"]["kind"] == "bool"
        assert fields["soft_clip_supplementary"]["group"] == "performance"
        assert fields["cs_mode"]["kind"] == "select"
        assert fields["cs_mode"]["group"] == "performance"
        assert [c["value"] for c in fields["cs_mode"]["choices"]] == [
            "none",
            "short",
            "long",
        ]
        assert fields["emit_md"]["kind"] == "bool"
        assert fields["emit_md"]["group"] == "performance"
        for key in (
            "preset",
            "kmer_size",
            "window_size",
            "min_chain_score",
            "max_gap",
            "secondary_ratio",
            "max_secondary",
            "secondary_mode",
            "batch_size",
            "soft_clip_supplementary",
            "cs_mode",
            "emit_md",
        ):
            assert fields[key]["help"].strip()

    def test_an_unknown_aligner_is_a_client_error(self, client):
        resp = client.get("/pipelines/aligners/not-real/schema")
        assert resp.status_code == 404
