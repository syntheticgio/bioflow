"""API surface for the install/uninstall endpoints.

`tool_install_service` is imported inside the route functions rather than at
module level (matching `suggestion_service`'s import style elsewhere in
pipelines.py), so it is monkeypatched on the *real* module,
`app.services.tool_install_service`, not on `app.api.v1.pipelines` -- a local
import re-resolves the name from `sys.modules` on every call, so it always
sees whatever is currently bound on the real module object. Patching a
`tool_install_service` attribute on the pipelines module itself would silently
do nothing, since no such attribute is ever created there.

Eligibility and dedup logic already have their own database-backed tests in
tests/services/test_tool_install_service.py; this file is about the wiring --
status codes, path routing, and the response shape -- not about re-testing
decisions that live one layer down.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.pipelines import router
from app.errors import ConflictError, NotFoundError, ValidationError, register_exception_handlers
from app.models import Job, JobClass, JobState
from app.services import tool_install_service
from tests.api.bare_app import override_owner


@pytest.fixture
def client():
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    override_owner(app)
    return TestClient(app)


def _fake_job(*, job_type: str, tool: str) -> Job:
    return Job(
        type=job_type,
        owner="test-owner",
        job_class=JobClass.USER_INTERACTIVE,
        state=JobState.PENDING,
        payload={"tool": tool},
    )


class TestInstallEndpoint:
    def test_success_returns_the_job(self, monkeypatch, client):
        async def fake_install(*, tool_name, owner):
            assert tool_name == "deepvariant"
            return _fake_job(job_type="install_tool", tool=tool_name)

        monkeypatch.setattr(tool_install_service, "install", fake_install)

        resp = client.post("/pipelines/tools/deepvariant/install")

        assert resp.status_code == 201
        body = resp.json()
        assert body["type"] == "install_tool"
        assert body["payload"]["tool"] == "deepvariant"

    def test_a_bundled_tool_is_rejected(self, monkeypatch, client):
        async def fake_install(*, tool_name, owner):
            raise ValidationError(f"{tool_name!r} is bundled")

        monkeypatch.setattr(tool_install_service, "install", fake_install)

        resp = client.post("/pipelines/tools/fastp/install")

        assert resp.status_code == 422

    def test_an_unknown_tool_is_not_found(self, monkeypatch, client):
        async def fake_install(*, tool_name, owner):
            raise NotFoundError(f"No such tool: {tool_name!r}")

        monkeypatch.setattr(tool_install_service, "install", fake_install)

        resp = client.post("/pipelines/tools/not-a-real-tool/install")

        assert resp.status_code == 404

    def test_an_in_flight_install_is_returned_not_duplicated(self, monkeypatch, client):
        """The endpoint's job is to hand back whatever the service decided --
        the actual dedup happens one layer down, tested at the service level.
        This just confirms a reused job round-trips through the API the same
        as a fresh one."""

        async def fake_install(*, tool_name, owner):
            return _fake_job(job_type="install_tool", tool=tool_name)

        monkeypatch.setattr(tool_install_service, "install", fake_install)

        first = client.post("/pipelines/tools/deepvariant/install")
        second = client.post("/pipelines/tools/deepvariant/install")

        assert first.status_code == second.status_code == 201


class TestUninstallEndpoint:
    def test_success_returns_the_job(self, monkeypatch, client):
        async def fake_uninstall(*, tool_name, owner):
            assert tool_name == "deepvariant"
            return _fake_job(job_type="uninstall_tool", tool=tool_name)

        monkeypatch.setattr(tool_install_service, "uninstall", fake_uninstall)

        resp = client.delete("/pipelines/tools/deepvariant/install")

        assert resp.status_code == 201
        assert resp.json()["type"] == "uninstall_tool"

    def test_a_running_job_using_the_tool_conflicts(self, monkeypatch, client):
        async def fake_uninstall(*, tool_name, owner):
            raise ConflictError(f"A running job is using {tool_name!r}")

        monkeypatch.setattr(tool_install_service, "uninstall", fake_uninstall)

        resp = client.delete("/pipelines/tools/deepvariant/install")

        assert resp.status_code == 409

    def test_not_installed_is_a_validation_error(self, monkeypatch, client):
        async def fake_uninstall(*, tool_name, owner):
            raise ValidationError(f"{tool_name!r} is not installed")

        monkeypatch.setattr(tool_install_service, "uninstall", fake_uninstall)

        resp = client.delete("/pipelines/tools/deepvariant/install")

        assert resp.status_code == 422

    def test_a_bundled_tool_is_rejected(self, monkeypatch, client):
        async def fake_uninstall(*, tool_name, owner):
            raise ValidationError(f"{tool_name!r} is bundled")

        monkeypatch.setattr(tool_install_service, "uninstall", fake_uninstall)

        resp = client.delete("/pipelines/tools/fastp/install")

        assert resp.status_code == 422
