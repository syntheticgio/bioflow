"""What /pipelines/summary/status answers now.

It used to mean "is the LM Studio server up?". It now means "is the provider
routed to file summaries usable?" -- and the answer differs by provider kind.
"""

import pytest
import pytest_asyncio
from app.models.ai import AiProvider, AiRouting, ProviderKind
from app.services.ai import crypto, provider_service

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest.fixture(autouse=True)
def key_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto.settings, "bioinfo_home", tmp_path)
    crypto._fernet.cache_clear()
    yield
    crypto._fernet.cache_clear()


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def clean():
    await AiProvider.find_all().delete()
    await AiRouting.find_all().delete()


async def _route(kind: ProviderKind, base_url: str, status: str = "untested"):
    p = await provider_service.create(
        name="P",
        kind=kind,
        base_url=base_url,
        model="m",
        api_key=None if kind == ProviderKind.OPENAI_COMPAT else "sk-x123456789",
    )
    if status != "untested":
        p.status = status
        await p.save()
    routing = await AiRouting.load()
    routing.default = str(p.id)
    await routing.save()
    return p


class TestSummaryStatus:
    async def test_nothing_configured(self, client):
        resp = await client.get("/api/v1/pipelines/summary/status")
        assert resp.status_code == 200
        assert resp.json() == {"available": False, "reason": "no_provider"}

    async def test_disabled_by_the_master_switch(self, client, monkeypatch):
        from app.api.v1 import pipelines

        await _route(ProviderKind.OPENAI_COMPAT, "http://host.docker.internal:1")
        monkeypatch.setattr(pipelines.settings, "llm_summaries_enabled", False)
        assert (await client.get("/api/v1/pipelines/summary/status")).json() == {
            "available": False,
            "reason": "disabled",
        }

    async def test_a_local_provider_is_probed_live(self, client, monkeypatch):
        """The local server is a process the user starts and stops by hand, so
        a remembered answer is the one most likely to be wrong."""
        from app.api.v1 import pipelines

        await _route(ProviderKind.OPENAI_COMPAT, "http://localhost:11234", status="ok")
        monkeypatch.setattr(pipelines, "_probe_local", lambda p: False)

        body = (await client.get("/api/v1/pipelines/summary/status")).json()
        assert body["available"] is False
        assert body["reason"] == "server_unavailable"

    async def test_a_hosted_provider_reports_stored_status(self, client, monkeypatch):
        """No network call: a hosted provider's failure mode is a bad key, not
        a down server, and that is not worth a round trip on every page load."""
        from app.api.v1 import pipelines

        def must_not_probe(p):
            raise AssertionError("hosted providers must not be probed")

        await _route(ProviderKind.ANTHROPIC, "https://api.anthropic.com", status="ok")
        monkeypatch.setattr(pipelines, "_probe_local", must_not_probe)

        body = (await client.get("/api/v1/pipelines/summary/status")).json()
        assert body["available"] is True
        assert body["provider_name"] == "P"

    async def test_a_failed_hosted_provider_is_unavailable(self, client):
        await _route(ProviderKind.ANTHROPIC, "https://api.anthropic.com", status="failed")
        body = (await client.get("/api/v1/pipelines/summary/status")).json()
        assert body["available"] is False
