"""API surface for salmon quantification requests.

Mirrors `test_pipelines_variant.py`: request-shape coverage plus
validation-only launch tests. This route has no settings dialog (unlike
`/quantify`, which `_CONFIGURE_DIALOGS` maps to QuantifyDialog and which
therefore also has a `/quantify/defaults/{id}` endpoint tested in
`test_pipelines_quantify.py`), so there is no defaults endpoint to cover
here -- the launch decisions themselves live at the service level.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.pipelines import SalmonQuantifyRequest, router
from app.errors import register_exception_handlers
from tests.api.bare_app import override_owner


@pytest.fixture
def client():
    """The owner dependency is overridden so body validation is what these
    tests actually reach. Without it every request 400s on the missing
    profile header before FastAPI parses the body, which would silently
    turn the 422 assertions below into assertions about `get_current_owner`.
    """
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    override_owner(app)
    return TestClient(app)


class TestSalmonQuantifyRequestShape:
    """The request model is the contract the card's launch body codes
    against."""

    def test_reads_id_is_the_only_required_field(self):
        req = SalmonQuantifyRequest(reads_id="000000000000000000000001")
        assert req.transcriptome_id is None
        assert req.mate_id is None
        assert req.params == {}
        assert req.resource_override is False

    def test_accepts_an_explicit_transcriptome(self):
        """The escape hatch for a project holding more than one distinct
        transcriptome, which the server refuses to guess between."""
        req = SalmonQuantifyRequest(
            reads_id="000000000000000000000001",
            transcriptome_id="000000000000000000000002",
        )
        assert req.transcriptome_id is not None

    def test_accepts_an_explicit_mate(self):
        req = SalmonQuantifyRequest(
            reads_id="000000000000000000000001",
            mate_id="000000000000000000000003",
        )
        assert req.mate_id is not None

    def test_rejects_a_malformed_id(self):
        with pytest.raises(ValueError):
            SalmonQuantifyRequest(reads_id="not-an-object-id")


class TestSalmonQuantifyEndpoint:
    """Request validation only. This endpoint reaches the database as soon
    as the body parses, and this client has none -- the not-found and
    launch paths belong at the service level, where the decisions
    (transcriptome resolution, mate pairing) actually live."""

    def test_launch_rejects_a_malformed_id(self, client):
        resp = client.post("/pipelines/salmon-quantify", json={"reads_id": "nope"})
        assert resp.status_code == 422

    def test_launch_requires_a_reads_id(self, client):
        resp = client.post("/pipelines/salmon-quantify", json={})
        assert resp.status_code == 422

    def test_launch_rejects_a_malformed_transcriptome_id(self, client):
        resp = client.post(
            "/pipelines/salmon-quantify",
            json={
                "reads_id": "000000000000000000000001",
                "transcriptome_id": "nope",
            },
        )
        assert resp.status_code == 422
