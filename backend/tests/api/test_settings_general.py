"""The general app settings surface.

Unscoped by profile, matching the AI and resource settings in the same
module: there is one machine here.
"""

import pytest
import pytest_asyncio

from app.models.app_settings import AppSettings

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def clean():
    await AppSettings.find_all().delete()


class TestGetGeneralSettings:
    async def test_a_fresh_install_has_feedback_disabled(self, client):
        resp = await client.get("/api/v1/settings/general")
        assert resp.status_code == 200
        assert resp.json()["feedback_enabled"] is False


class TestPutGeneralSettings:
    async def test_it_enables_feedback(self, client):
        resp = await client.put(
            "/api/v1/settings/general", json={"feedback_enabled": True}
        )
        assert resp.status_code == 200
        assert resp.json()["feedback_enabled"] is True

        again = await client.get("/api/v1/settings/general")
        assert again.json()["feedback_enabled"] is True

    async def test_it_disables_feedback_again(self, client):
        await client.put("/api/v1/settings/general", json={"feedback_enabled": True})
        resp = await client.put(
            "/api/v1/settings/general", json={"feedback_enabled": False}
        )
        assert resp.status_code == 200
        assert resp.json()["feedback_enabled"] is False
