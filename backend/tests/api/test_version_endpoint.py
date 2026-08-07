"""The version endpoint: what the About page and a support conversation read.

The launcher's update check compares image *digests*, not versions, so without
this a user running `:latest` has no way to discover what they have.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.version import __version__

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


class TestVersionEndpoint:
    async def test_returns_the_running_version(self, client):
        r = await client.get("/api/v1/version")

        assert r.status_code == 200
        assert r.json() == {"version": __version__}

    async def test_needs_no_profile_or_auth(self, client):
        """It must answer before anything is configured -- a user asking "what
        am I running?" may be mid-setup with no profile selected."""
        r = await client.get("/api/v1/version")
        assert r.status_code == 200
