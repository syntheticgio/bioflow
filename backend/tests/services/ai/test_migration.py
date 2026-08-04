"""Seeding a provider from the pre-settings environment variables.

Without this, the first run after this ships silently stops producing summaries
on an installation that was working -- the base URL moved from config into a
document that does not exist yet.
"""

import pytest
import pytest_asyncio

from app.models.ai import AiProvider, AiRouting, ProviderKind
from app.services.ai import crypto, migration, provider_service

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


class TestSeedLegacyProvider:
    async def test_creates_a_provider_from_the_legacy_url(self, monkeypatch):
        monkeypatch.setattr(
            migration.settings, "ai_legacy_base_url", "http://host.docker.internal:11234"
        )
        await migration.seed_legacy_provider()

        providers = await provider_service.list_all()
        assert len(providers) == 1
        assert providers[0].base_url == "http://host.docker.internal:11234"
        assert providers[0].kind == ProviderKind.OPENAI_COMPAT
        assert providers[0].api_key_enc is None

    async def test_points_the_default_at_it(self, monkeypatch):
        """Seeding a provider nothing routes to would leave the install just as
        broken as seeding nothing."""
        monkeypatch.setattr(migration.settings, "ai_legacy_base_url", "http://x:1234")
        await migration.seed_legacy_provider()

        routing = await AiRouting.load()
        providers = await provider_service.list_all()
        assert routing.default == str(providers[0].id)

    async def test_carries_the_legacy_model_when_pinned(self, monkeypatch):
        monkeypatch.setattr(migration.settings, "ai_legacy_base_url", "http://x:1234")
        monkeypatch.setattr(migration.settings, "ai_legacy_model", "pinned-model")
        await migration.seed_legacy_provider()

        assert (await provider_service.list_all())[0].model == "pinned-model"

    async def test_does_nothing_when_providers_already_exist(self, monkeypatch):
        """Idempotent: this runs on every startup, and a second provider named
        'Local' would collide on the unique index anyway."""
        await provider_service.create(
            name="Existing", kind=ProviderKind.OPENAI_COMPAT,
            base_url="http://y:1", model="m", api_key=None,
        )
        monkeypatch.setattr(migration.settings, "ai_legacy_base_url", "http://x:1234")

        await migration.seed_legacy_provider()

        providers = await provider_service.list_all()
        assert len(providers) == 1
        assert providers[0].name == "Existing"

    async def test_does_nothing_when_no_legacy_url_is_set(self, monkeypatch):
        """A fresh install with no history gets an empty settings page, not a
        provider pointing at a port nothing is listening on."""
        monkeypatch.setattr(migration.settings, "ai_legacy_base_url", "")
        await migration.seed_legacy_provider()
        assert await provider_service.list_all() == []
