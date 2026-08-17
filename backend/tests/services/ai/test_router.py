"""Slot resolution: override wins, then default, then nothing."""

import pytest
import pytest_asyncio

from app.models.ai import AiProvider, AiRouting, ProviderKind, TaskSlot
from app.services.ai import crypto, provider_service, router

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


async def _provider(name: str, key: str | None = None) -> AiProvider:
    return await provider_service.create(
        name=name, kind=ProviderKind.OPENAI_COMPAT,
        base_url="http://x:1", model="m", api_key=key,
    )


class TestResolve:
    async def test_a_slot_override_wins_over_the_default(self):
        default = await _provider("Default")
        special = await _provider("Special")
        routing = await AiRouting.load()
        routing.default = str(default.id)
        routing.slots = {TaskSlot.ORGANISM_BLURB.value: str(special.id)}
        await routing.save()

        resolved = await router.resolve(TaskSlot.ORGANISM_BLURB)
        assert resolved.name == "Special"

    async def test_an_unassigned_slot_falls_back_to_the_default(self):
        default = await _provider("Default")
        routing = await AiRouting.load()
        routing.default = str(default.id)
        await routing.save()

        resolved = await router.resolve(TaskSlot.FILE_SUMMARY)
        assert resolved.name == "Default"

    async def test_no_default_and_no_override_resolves_to_none(self):
        """A fresh install with nothing configured. Callers treat None as the
        same non-event as a model server being off."""
        assert await router.resolve(TaskSlot.FILE_SUMMARY) is None

    async def test_a_dangling_slot_id_resolves_to_none(self):
        """Deleting clears routing, so this should not happen -- but a hand-
        edited database should degrade to 'nothing configured', not a 500."""
        from bson import ObjectId

        routing = await AiRouting.load()
        routing.slots = {TaskSlot.FILE_SUMMARY.value: str(ObjectId())}
        await routing.save()
        assert await router.resolve(TaskSlot.FILE_SUMMARY) is None

    async def test_decrypts_the_key(self):
        p = await _provider("Keyed", key="sk-secret123456")
        routing = await AiRouting.load()
        routing.default = str(p.id)
        await routing.save()

        resolved = await router.resolve(TaskSlot.FILE_SUMMARY)
        assert resolved.api_key == "sk-secret123456"

    async def test_keyless_provider_resolves_with_no_key(self):
        p = await _provider("Local")
        routing = await AiRouting.load()
        routing.default = str(p.id)
        await routing.save()

        resolved = await router.resolve(TaskSlot.FILE_SUMMARY)
        assert resolved.api_key is None

    async def test_carries_the_provider_id_for_failure_recording(self):
        """complete() writes the failure reason back onto the provider, so the
        resolved value has to know which one it came from."""
        p = await _provider("Local")
        routing = await AiRouting.load()
        routing.default = str(p.id)
        await routing.save()

        resolved = await router.resolve(TaskSlot.FILE_SUMMARY)
        assert resolved.provider_id == str(p.id)

    async def test_a_provider_with_no_model_still_resolves(self):
        """The model can come from the cache's first entry at call time; an
        unset model is a nudge to configure, not a hard stop."""
        p = await provider_service.create(
            name="NoModel", kind=ProviderKind.OPENAI_COMPAT,
            base_url="http://x:1", model="", api_key=None,
        )
        routing = await AiRouting.load()
        routing.default = str(p.id)
        await routing.save()

        resolved = await router.resolve(TaskSlot.FILE_SUMMARY)
        assert resolved is not None
        assert resolved.model == ""
