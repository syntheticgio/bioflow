"""The resource-limit settings surface.

Unscoped by profile, matching the AI settings in the same module: there is one
machine here, so a profile header cannot change how much memory it has.
"""

import pytest
import pytest_asyncio

from app.models.resource_limits import ResourceLimits

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def clean():
    await ResourceLimits.find_all().delete()


class TestGetLimits:
    async def test_a_fresh_install_reports_no_limits(self, client):
        resp = await client.get("/api/v1/settings/resources")
        assert resp.status_code == 200
        body = resp.json()
        assert body["max_mem_mb"] is None
        assert body["max_cpu"] is None
        assert body["max_threads"] is None

    async def test_it_reports_the_machine_budget_alongside(self, client):
        """The UI needs the host's actual capacity to render a sensible
        slider range and to say what "no limit" currently resolves to."""
        resp = await client.get("/api/v1/settings/resources")
        body = resp.json()
        assert body["machine_mem_mb"] > 0
        assert body["machine_cpu"] > 0

    async def test_it_reports_the_hard_limit_when_one_is_configured(self, client, monkeypatch):
        monkeypatch.setattr("app.config.settings.bioflow_hard_mem_mb", 16384)
        resp = await client.get("/api/v1/settings/resources")
        assert resp.json()["hard_mem_mb"] == 16384


class TestPutLimits:
    async def test_it_stores_a_limit(self, client):
        resp = await client.put(
            "/api/v1/settings/resources",
            json={"max_mem_mb": 8192, "max_cpu": 4, "max_threads": 8},
        )
        assert resp.status_code == 200
        assert resp.json()["max_mem_mb"] == 8192

        again = await client.get("/api/v1/settings/resources")
        assert again.json()["max_mem_mb"] == 8192

    async def test_null_clears_a_previously_set_limit(self, client):
        """"No limit" must be able to undo a limit. An absent value means no
        limit rather than 'leave unchanged' -- there is no secret to preserve
        here, unlike the AI provider key."""
        await client.put(
            "/api/v1/settings/resources",
            json={"max_mem_mb": 8192, "max_cpu": None, "max_threads": None},
        )
        await client.put(
            "/api/v1/settings/resources",
            json={"max_mem_mb": None, "max_cpu": None, "max_threads": None},
        )
        resp = await client.get("/api/v1/settings/resources")
        assert resp.json()["max_mem_mb"] is None

    async def test_it_rejects_a_zero_or_negative_memory_limit(self, client):
        """A literal zero budget would admit no job ever and stall the queue
        with no error anywhere. Refused at the edge rather than silently
        reinterpreted, so the user learns their input was meaningless."""
        resp = await client.put(
            "/api/v1/settings/resources",
            json={"max_mem_mb": 0, "max_cpu": None, "max_threads": None},
        )
        assert resp.status_code == 422

    async def test_it_rejects_a_soft_budget_above_the_hard_limit(self, client, monkeypatch):
        """Without this, admission would approve a budget the kernel then
        kills every job for -- the worst version of this feature."""
        monkeypatch.setattr("app.config.settings.bioflow_hard_mem_mb", 16384)
        resp = await client.put(
            "/api/v1/settings/resources",
            json={"max_mem_mb": 32768, "max_cpu": None, "max_threads": None},
        )
        assert resp.status_code == 422
        assert "16384" in resp.json()["detail"]

    async def test_a_soft_budget_below_the_hard_limit_is_accepted(self, client, monkeypatch):
        monkeypatch.setattr("app.config.settings.bioflow_hard_mem_mb", 16384)
        resp = await client.put(
            "/api/v1/settings/resources",
            json={"max_mem_mb": 8192, "max_cpu": None, "max_threads": None},
        )
        assert resp.status_code == 200
