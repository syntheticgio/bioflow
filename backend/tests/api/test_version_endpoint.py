"""The version endpoint: what the About page and a support conversation read.

The launcher's update check compares image *digests*, not versions, so without
this a user running `:latest` has no way to discover what they have.
"""

import pytest
from app.main import app
from app.version import __version__
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


class TestVersionEndpoint:
    async def test_returns_the_running_version(self, client):
        r = await client.get("/api/v1/version")

        assert r.status_code == 200
        assert r.json()["version"] == __version__

    async def test_needs_no_profile_or_auth(self, client):
        """It must answer before anything is configured -- a user asking "what
        am I running?" may be mid-setup with no profile selected."""
        r = await client.get("/api/v1/version")
        assert r.status_code == 200

    async def test_reports_the_served_checkout_when_there_is_one(
        self, client, tmp_path, monkeypatch
    ):
        """`version` alone cannot answer "is this stale?" -- the api serves
        bind-mounted source, so the branch is the thing that decides (#452)."""
        git = tmp_path / ".git"
        git.mkdir()
        (git / "HEAD").write_text("ref: refs/heads/fix/394-pin-node-ssh-host-keys\n")
        (git / "refs" / "heads" / "fix").mkdir(parents=True)
        (git / "refs" / "heads" / "fix" / "394-pin-node-ssh-host-keys").write_text(
            "95ed6733" + "0" * 32
        )
        (git / "refs" / "remotes" / "origin").mkdir(parents=True)
        (git / "refs" / "remotes" / "origin" / "main").write_text("df67a0f4" + "0" * 32)
        monkeypatch.setattr("app.services.git_revision.GIT_DIR", git)

        body = (await client.get("/api/v1/version")).json()

        assert body["git_sha"] == "95ed673"
        assert body["git_branch"] == "fix/394-pin-node-ssh-host-keys"
        assert body["git_matches_origin_main"] is False

    async def test_omits_revision_fields_when_running_a_shipped_image(
        self, client, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("app.services.git_revision.GIT_DIR", tmp_path / "absent")

        body = (await client.get("/api/v1/version")).json()

        assert body == {
            "version": __version__,
            "git_sha": None,
            "git_branch": None,
            "git_matches_origin_main": None,
        }
