"""The settings endpoints.

The test that matters most is `TestKeysNeverLeak` -- it asserts the security
property the whole design rests on, across every response shape.
"""

import pytest
import pytest_asyncio

from app.models.ai import AiProvider, AiRouting, ProviderKind, TaskSlot
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


SECRET = "sk-ant-supersecret9999"


class TestPresets:
    async def test_lists_presets(self, client):
        resp = await client.get("/api/v1/settings/ai/presets")
        assert resp.status_code == 200
        ids = {p["id"] for p in resp.json()}
        assert "anthropic" in ids
        assert "local" in ids


class TestCreate:
    async def test_creates_a_provider(self, client):
        resp = await client.post(
            "/api/v1/settings/ai/providers",
            json={
                "name": "Anthropic",
                "kind": "anthropic",
                "base_url": "https://api.anthropic.com",
                "model": "claude-x",
                "api_key": SECRET,
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["has_key"] is True
        assert resp.json()["key_hint"] == "sk-ant-…9999"

    async def test_rejects_a_duplicate_name(self, client):
        body = {"name": "A", "kind": "openai_compat", "base_url": "http://x:1", "model": "m"}
        assert (await client.post("/api/v1/settings/ai/providers", json=body)).status_code == 201
        assert (await client.post("/api/v1/settings/ai/providers", json=body)).status_code == 409


class TestUpdate:
    async def test_omitting_the_key_preserves_it(self, client):
        created = await client.post(
            "/api/v1/settings/ai/providers",
            json={"name": "A", "kind": "anthropic", "base_url": "https://x",
                  "model": "m", "api_key": SECRET},
        )
        pid = created.json()["id"]

        resp = await client.patch(
            f"/api/v1/settings/ai/providers/{pid}", json={"model": "m2"}
        )
        assert resp.status_code == 200
        assert resp.json()["has_key"] is True

        stored = await provider_service.get(pid)
        assert crypto.decrypt(stored.api_key_enc) == SECRET

    async def test_explicit_null_clears_the_key(self, client):
        created = await client.post(
            "/api/v1/settings/ai/providers",
            json={"name": "A", "kind": "anthropic", "base_url": "https://x",
                  "model": "m", "api_key": SECRET},
        )
        pid = created.json()["id"]
        resp = await client.patch(
            f"/api/v1/settings/ai/providers/{pid}", json={"api_key": None}
        )
        assert resp.json()["has_key"] is False

    async def test_unknown_id_is_404(self, client):
        from bson import ObjectId

        resp = await client.patch(
            f"/api/v1/settings/ai/providers/{ObjectId()}", json={"model": "m"}
        )
        assert resp.status_code == 404


class TestDelete:
    async def test_deletes(self, client):
        created = await client.post(
            "/api/v1/settings/ai/providers",
            json={"name": "A", "kind": "openai_compat", "base_url": "http://x:1", "model": "m"},
        )
        pid = created.json()["id"]
        assert (await client.delete(f"/api/v1/settings/ai/providers/{pid}")).status_code == 204
        assert (await client.get("/api/v1/settings/ai/providers")).json() == []


class TestFetchModels:
    async def test_returns_the_model_list(self, client, monkeypatch):
        created = await client.post(
            "/api/v1/settings/ai/providers",
            json={"name": "A", "kind": "openai_compat", "base_url": "http://x:1", "model": ""},
        )
        pid = created.json()["id"]
        monkeypatch.setattr(provider_service, "_list_models", lambda p: ["m1", "m2"])

        resp = await client.post(f"/api/v1/settings/ai/providers/{pid}/fetch-models")
        assert resp.status_code == 200
        assert resp.json()["models"] == ["m1", "m2"]
        assert resp.json()["status"] == "ok"

    async def test_reports_a_failure_without_erroring(self, client, monkeypatch):
        """A 200 with a failure inside, not a 502: the request succeeded, the
        provider is what failed, and the UI renders that as a badge."""
        from app.models.ai import FailureReason
        from app.services.ai.adapters import Failure

        created = await client.post(
            "/api/v1/settings/ai/providers",
            json={"name": "A", "kind": "openai_compat", "base_url": "http://x:1", "model": ""},
        )
        pid = created.json()["id"]
        monkeypatch.setattr(
            provider_service,
            "_list_models",
            lambda p: Failure(FailureReason.INVALID_KEY, "bad key"),
        )
        resp = await client.post(f"/api/v1/settings/ai/providers/{pid}/fetch-models")
        assert resp.status_code == 200
        assert resp.json()["status"] == "failed"
        assert resp.json()["reason"] == "invalid_key"


class TestRouting:
    async def test_returns_the_slot_catalog(self, client):
        """The UI must not hardcode slot names or labels."""
        resp = await client.get("/api/v1/settings/ai/routing")
        assert resp.status_code == 200
        slots = {s["name"]: s["label"] for s in resp.json()["catalog"]}
        assert slots["file_summary"] == "File summaries"
        assert slots["organism_blurb"] == "Organism blurbs"

    async def test_sets_the_default_and_a_slot(self, client):
        created = await client.post(
            "/api/v1/settings/ai/providers",
            json={"name": "A", "kind": "openai_compat", "base_url": "http://x:1", "model": "m"},
        )
        pid = created.json()["id"]

        resp = await client.put(
            "/api/v1/settings/ai/routing",
            json={"default": pid, "slots": {"organism_blurb": pid}},
        )
        assert resp.status_code == 200
        assert resp.json()["default"] == pid
        assert resp.json()["slots"]["organism_blurb"] == pid

    async def test_rejects_an_unknown_slot_name(self, client):
        resp = await client.put(
            "/api/v1/settings/ai/routing", json={"default": None, "slots": {"nope": "x"}}
        )
        assert resp.status_code == 422

    async def test_rejects_an_unknown_provider_id(self, client):
        from bson import ObjectId

        resp = await client.put(
            "/api/v1/settings/ai/routing", json={"default": str(ObjectId()), "slots": {}}
        )
        assert resp.status_code == 422

    async def test_reports_which_slots_use_each_provider(self, client):
        """The 'Used by' line, which is what stops master-detail hiding the
        routing behind a click."""
        created = await client.post(
            "/api/v1/settings/ai/providers",
            json={"name": "A", "kind": "openai_compat", "base_url": "http://x:1", "model": "m"},
        )
        pid = created.json()["id"]
        await client.put(
            "/api/v1/settings/ai/routing",
            json={"default": None, "slots": {"organism_blurb": pid}},
        )
        listing = (await client.get("/api/v1/settings/ai/providers")).json()
        assert listing[0]["used_by"] == ["Organism blurbs"]


class TestKeysNeverLeak:
    async def test_no_settings_response_contains_a_full_key(self, client, monkeypatch):
        """The security property the design rests on, asserted across every
        response shape rather than only the obvious one."""
        created = await client.post(
            "/api/v1/settings/ai/providers",
            json={"name": "A", "kind": "anthropic", "base_url": "https://x",
                  "model": "m", "api_key": SECRET},
        )
        pid = created.json()["id"]
        monkeypatch.setattr(provider_service, "_list_models", lambda p: ["m1"])

        responses = [
            created,
            await client.get("/api/v1/settings/ai/providers"),
            await client.patch(f"/api/v1/settings/ai/providers/{pid}", json={"model": "m2"}),
            await client.post(f"/api/v1/settings/ai/providers/{pid}/fetch-models"),
            await client.get("/api/v1/settings/ai/routing"),
            await client.get("/api/v1/settings/ai/presets"),
        ]
        for resp in responses:
            assert SECRET not in resp.text, resp.url
