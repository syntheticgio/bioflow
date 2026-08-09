"""Serving the per-tile quality matrix as JSON.

Same guards as `get_qc_report` (ownership check, path containment) but a
different response shape and, deliberately, no sandbox CSP -- see the route's
own docstring for why. Fixture pattern matches test_qc_reports.py: a bare app
with no database, since this route also touches only the filesystem.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import pipelines as pipelines_api
from app.api.v1.pipelines import router
from app.config import settings
from app.errors import register_exception_handlers
from tests.api.bare_app import override_owner, stub_get_object

OBJECT_ID = "507f1f77bcf86cd799439011"
OTHER_ID = "507f191e810c19729de860ea"
UNKNOWN_ID = "5f1f1f1f1f1f1f1f1f1f1f1f"

MATRIX_PAYLOAD = {"tiles": [1101, 1102], "positions": 2, "matrix": [[40.0, 39.0], [30.0, 29.0]]}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "bioinfo_home", tmp_path)

    reports = tmp_path / "qc_reports"
    (reports / OBJECT_ID).mkdir(parents=True)
    (reports / OBJECT_ID / "tile_quality.json").write_text(json.dumps(MATRIX_PAYLOAD))

    stub_get_object(monkeypatch, pipelines_api, known={OBJECT_ID, OTHER_ID})

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    override_owner(app)
    return TestClient(app)


def test_returns_the_matrix(client):
    # Full round-trip against the fixture's exact payload, not just one key --
    # a regression that dropped or mangled `matrix`/`positions` would pass a
    # narrower "tiles looks right" assertion.
    res = client.get(f"/pipelines/qc/tiles/{OBJECT_ID}")
    assert res.status_code == 200
    assert res.json() == MATRIX_PAYLOAD


def test_missing_matrix_is_a_404(client, tmp_path):
    # Object exists (known to the stub) but never had a tile scan -- the
    # ordinary case for a file whose QC predates this feature, or whose
    # headers had no tiles.
    (tmp_path / "qc_reports" / OBJECT_ID / "tile_quality.json").unlink()
    res = client.get(f"/pipelines/qc/tiles/{OBJECT_ID}")
    assert res.status_code == 404


def test_unknown_object_is_a_404(client):
    res = client.get(f"/pipelines/qc/tiles/{UNKNOWN_ID}")
    assert res.status_code == 404


def test_response_is_plain_json_not_sandboxed_html(client):
    # The sandbox CSP on get_qc_report would block the frontend's own fetch
    # of this endpoint -- this route must not carry it.
    res = client.get(f"/pipelines/qc/tiles/{OBJECT_ID}")
    assert "application/json" in res.headers["content-type"]
    assert "sandbox" not in res.headers.get("content-security-policy", "")


class TestPathTraversal:
    @pytest.mark.parametrize(
        "attack",
        ["../../../etc/passwd", "/etc/passwd"],
    )
    async def test_the_handler_itself_refuses_a_dotdot_or_absolute_target(
        self, client, attack, tmp_path, monkeypatch
    ):
        """The filename served is a module constant (TILE_MATRIX_FILENAME),
        never user input, so a traversal is not reachable through this route's
        URL the way it is through get_qc_report's path-carrying one. This
        test instead proves the belt-and-braces resolve-and-recheck inside the
        handler still holds if that ever changes -- calling the handler
        directly with a monkeypatched filename, the same way
        test_qc_reports.py tests get_qc_report's guard directly."""
        from app.api.v1 import pipelines as pipelines_module
        from app.errors import NotFoundError
        from app.pipelines import tile_scanner
        from tests.api.bare_app import TEST_OWNER

        monkeypatch.setattr(tile_scanner, "TILE_MATRIX_FILENAME", attack)
        with pytest.raises(NotFoundError):
            await pipelines_module.get_qc_tile_matrix(OBJECT_ID, TEST_OWNER)
