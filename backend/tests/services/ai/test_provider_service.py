"""Provider CRUD. The key-preservation test is the one that matters most --
its failure silently destroys a credential.
"""

import pytest
import pytest_asyncio
from app.models.ai import AiProvider, AiRouting, FailureReason, ProviderKind, TaskSlot
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


class TestCreate:
    async def test_stores_the_key_encrypted(self):
        p = await provider_service.create(
            name="Anthropic",
            kind=ProviderKind.ANTHROPIC,
            base_url="https://api.anthropic.com",
            model="claude-x",
            api_key="sk-ant-secret12345",
        )
        assert p.api_key_enc is not None
        assert b"secret" not in p.api_key_enc

    async def test_stores_a_hint(self):
        p = await provider_service.create(
            name="A", kind=ProviderKind.ANTHROPIC, base_url="https://x",
            model="m", api_key="sk-ant-secret12345",
        )
        assert p.key_hint == "sk-ant-…2345"

    async def test_keyless_provider_has_no_hint(self):
        p = await provider_service.create(
            name="Local", kind=ProviderKind.OPENAI_COMPAT,
            base_url="http://x:1", model="m", api_key=None,
        )
        assert p.api_key_enc is None
        assert p.key_hint is None


class TestUpdate:
    async def test_omitted_key_preserves_the_stored_one(self):
        """The single most important behaviour here. The UI submits the form
        without an api_key unless the user typed a new one, so if this were
        wrong, editing the model name would wipe the credential."""
        p = await provider_service.create(
            name="A", kind=ProviderKind.ANTHROPIC, base_url="https://x",
            model="m", api_key="sk-ant-original123",
        )
        before = p.api_key_enc

        updated = await provider_service.update(str(p.id), {"model": "m2"})

        assert updated.api_key_enc == before
        assert updated.key_hint == "sk-ant-…l123"
        assert crypto.decrypt(updated.api_key_enc) == "sk-ant-original123"

    async def test_explicit_none_clears_the_key(self):
        p = await provider_service.create(
            name="A", kind=ProviderKind.ANTHROPIC, base_url="https://x",
            model="m", api_key="sk-ant-original123",
        )
        updated = await provider_service.update(str(p.id), {"api_key": None})
        assert updated.api_key_enc is None
        assert updated.key_hint is None

    async def test_a_new_key_replaces_the_old(self):
        p = await provider_service.create(
            name="A", kind=ProviderKind.ANTHROPIC, base_url="https://x",
            model="m", api_key="sk-ant-original123",
        )
        updated = await provider_service.update(str(p.id), {"api_key": "sk-ant-replaced99"})
        assert crypto.decrypt(updated.api_key_enc) == "sk-ant-replaced99"

    async def test_unknown_id_returns_none(self):
        from bson import ObjectId

        assert await provider_service.update(str(ObjectId()), {"model": "m"}) is None


class TestDelete:
    async def test_clears_slots_routed_to_the_deleted_provider(self):
        """Refusing the delete would mean an error telling the user to go undo
        three things first. Clearing to default is the kinder equivalent."""
        p = await provider_service.create(
            name="A", kind=ProviderKind.OPENAI_COMPAT, base_url="http://x:1",
            model="m", api_key=None,
        )
        routing = await AiRouting.load()
        routing.slots[TaskSlot.FILE_SUMMARY.value] = str(p.id)
        await routing.save()

        await provider_service.delete(str(p.id))

        after = await AiRouting.load()
        assert TaskSlot.FILE_SUMMARY.value not in after.slots

    async def test_clears_the_default_when_it_is_deleted(self):
        p = await provider_service.create(
            name="A", kind=ProviderKind.OPENAI_COMPAT, base_url="http://x:1",
            model="m", api_key=None,
        )
        routing = await AiRouting.load()
        routing.default = str(p.id)
        await routing.save()

        await provider_service.delete(str(p.id))

        assert (await AiRouting.load()).default is None

    async def test_leaves_other_slots_alone(self):
        keep = await provider_service.create(
            name="Keep", kind=ProviderKind.OPENAI_COMPAT, base_url="http://x:1",
            model="m", api_key=None,
        )
        drop = await provider_service.create(
            name="Drop", kind=ProviderKind.OPENAI_COMPAT, base_url="http://x:2",
            model="m", api_key=None,
        )
        routing = await AiRouting.load()
        routing.slots = {
            TaskSlot.FILE_SUMMARY.value: str(keep.id),
            TaskSlot.ORGANISM_BLURB.value: str(drop.id),
        }
        await routing.save()

        await provider_service.delete(str(drop.id))

        after = await AiRouting.load()
        assert after.slots == {TaskSlot.FILE_SUMMARY.value: str(keep.id)}


class TestFetchModels:
    async def test_success_caches_the_list_and_marks_ok(self, monkeypatch):
        p = await provider_service.create(
            name="A", kind=ProviderKind.OPENAI_COMPAT, base_url="http://x:1",
            model="", api_key=None,
        )
        monkeypatch.setattr(
            provider_service, "_list_models", lambda prov: ["alpha", "zeta"]
        )
        models = await provider_service.fetch_models(str(p.id))

        assert models == ["alpha", "zeta"]
        refreshed = await AiProvider.get(p.id)
        assert refreshed.models_cache == ["alpha", "zeta"]
        assert refreshed.status == "ok"
        assert refreshed.checked_at is not None

    async def test_success_also_caches_context_windows(self, monkeypatch):
        p = await provider_service.create(
            name="A", kind=ProviderKind.OPENAI_COMPAT, base_url="http://x:1",
            model="", api_key=None,
        )
        monkeypatch.setattr(
            provider_service, "_list_models", lambda prov: ["alpha", "zeta"]
        )
        monkeypatch.setattr(
            provider_service,
            "_list_models_with_context",
            lambda prov: {"alpha": 32000, "zeta": None},
        )

        await provider_service.fetch_models(str(p.id))

        refreshed = await AiProvider.get(p.id)
        assert refreshed.context_windows == {"alpha": 32000}

    async def test_failure_keeps_the_previous_cache(self, monkeypatch):
        """A listing endpoint having a bad day must not empty the dropdown."""
        from app.services.ai.adapters import Failure

        p = await provider_service.create(
            name="A", kind=ProviderKind.OPENAI_COMPAT, base_url="http://x:1",
            model="", api_key=None,
        )
        p.models_cache = ["previously-fetched"]
        await p.save()

        monkeypatch.setattr(
            provider_service,
            "_list_models",
            lambda prov: Failure(FailureReason.INVALID_KEY, "nope"),
        )
        result = await provider_service.fetch_models(str(p.id))

        assert isinstance(result, Failure)
        refreshed = await AiProvider.get(p.id)
        assert refreshed.models_cache == ["previously-fetched"]
        assert refreshed.status == "failed"
        assert refreshed.status_reason == FailureReason.INVALID_KEY
