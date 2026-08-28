"""Provider CRUD. The key-preservation test is the one that matters most --
its failure silently destroys a credential.
"""

import pytest
import pytest_asyncio

from app.errors import ValidationError
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


class TestBaseUrlChangeInvalidatesTheKey:
    """A key is a credential for a host, so it must not follow the host (#870/#872).

    The API is unauthenticated, so without this anyone who could reach it --
    including a DNS-rebinding page -- could repoint an existing provider at a
    host they control and have the stored key delivered there in an
    Authorization header on the next completion.
    """

    async def test_changing_the_base_url_drops_the_stored_key(self):
        p = await provider_service.create(
            name="A", kind=ProviderKind.ANTHROPIC, base_url="https://api.anthropic.com",
            model="m", api_key="sk-ant-original123",
        )
        updated = await provider_service.update(
            str(p.id), {"base_url": "http://attacker.example:8080"}
        )
        assert updated.api_key_enc is None
        assert updated.key_hint is None
        # The move itself still happened -- this invalidates the credential, it
        # does not refuse the edit.
        assert updated.base_url == "http://attacker.example:8080"

    async def test_a_key_re_entered_in_the_same_request_is_kept(self):
        """The ordinary "move it and re-key it in one submit" edit. A key typed
        in this request was typed for the *new* host, so dropping it would make
        the fix unusable rather than safe."""
        p = await provider_service.create(
            name="A", kind=ProviderKind.OPENAI_COMPAT, base_url="http://old:1",
            model="m", api_key="sk-old-000000000",
        )
        updated = await provider_service.update(
            str(p.id), {"base_url": "http://new:2", "api_key": "sk-new-111111111"}
        )
        assert crypto.decrypt(updated.api_key_enc) == "sk-new-111111111"
        assert updated.base_url == "http://new:2"

    async def test_resubmitting_the_same_base_url_preserves_the_key(self):
        """The form submits every field, so an unrelated edit resends base_url
        unchanged. Treating that as a move would wipe the credential on every
        rename -- the exact regression test_omitted_key_preserves_the_stored_one
        exists to prevent, arriving by a different door."""
        p = await provider_service.create(
            name="A", kind=ProviderKind.ANTHROPIC, base_url="https://x",
            model="m", api_key="sk-ant-original123",
        )
        updated = await provider_service.update(
            str(p.id), {"base_url": "https://x", "name": "Renamed"}
        )
        assert crypto.decrypt(updated.api_key_enc) == "sk-ant-original123"
        assert updated.name == "Renamed"

    async def test_a_keyless_provider_moves_without_incident(self):
        """A local Ollama has no key; the drop branch must be a no-op there."""
        p = await provider_service.create(
            name="Local", kind=ProviderKind.OPENAI_COMPAT,
            base_url="http://host.docker.internal:11434", model="m", api_key=None,
        )
        updated = await provider_service.update(
            str(p.id), {"base_url": "http://host.docker.internal:8080"}
        )
        assert updated.api_key_enc is None
        assert updated.base_url == "http://host.docker.internal:8080"


class TestBaseUrlValidation:
    """Only the scheme is constrained. Pointing a provider at your own local
    model server is the intended feature, so a host allowlist would break the
    thing the setting is for."""

    @pytest.mark.parametrize(
        "base_url",
        [
            "http://localhost:11434",
            "http://127.0.0.1:11434",
            "http://host.docker.internal:11434",
            "https://api.anthropic.com",
            "http://192.168.1.5:8080",
        ],
    )
    async def test_ordinary_and_local_urls_are_accepted(self, base_url):
        p = await provider_service.create(
            name="A", kind=ProviderKind.OPENAI_COMPAT, base_url=base_url,
            model="m", api_key=None,
        )
        assert p.base_url == base_url

    @pytest.mark.parametrize(
        "base_url",
        [
            "file:///etc/passwd",  # urllib would read the container's filesystem
            "gopher://evil:70/_x",  # classic SSRF protocol smuggling
            "ftp://evil/x",
            "http://",  # scheme but no host
            "not-a-url",
            "",
        ],
    )
    async def test_bad_urls_are_refused_on_create(self, base_url):
        with pytest.raises(ValidationError):
            await provider_service.create(
                name="A", kind=ProviderKind.OPENAI_COMPAT, base_url=base_url,
                model="m", api_key=None,
            )

    async def test_bad_urls_are_refused_on_update(self):
        """Both doors, not just create -- update is the one the report is about."""
        p = await provider_service.create(
            name="A", kind=ProviderKind.OPENAI_COMPAT, base_url="http://ok:1",
            model="m", api_key=None,
        )
        with pytest.raises(ValidationError):
            await provider_service.update(str(p.id), {"base_url": "file:///etc/passwd"})


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
