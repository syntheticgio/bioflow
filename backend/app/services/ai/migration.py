"""Carry the pre-settings configuration into the database, once.

Before this feature the model server was `LLM_BASE_URL` in the environment.
After it, providers are documents. An installation that was working must keep
working without anyone opening the settings page -- so on the first startup
where no providers exist, the old environment values become one.

Runs on every startup and does nothing after the first: the collection is empty
exactly once.
"""

from app.config import settings
from app.logging import get_logger
from app.models.ai import AiRouting, ProviderKind
from app.services.ai import provider_service

log = get_logger(__name__)


async def seed_legacy_provider() -> None:
    if not settings.ai_legacy_base_url:
        # A fresh install. An empty settings page is the honest state; a
        # seeded provider pointing at a port nothing is listening on is not.
        return

    existing = await provider_service.list_all()
    if existing:
        return

    provider = await provider_service.create(
        name="Local",
        kind=ProviderKind.OPENAI_COMPAT,
        base_url=settings.ai_legacy_base_url,
        model=settings.ai_legacy_model,
        api_key=None,
    )
    routing = await AiRouting.load()
    routing.default = str(provider.id)
    await routing.save()
    log.info("ai_legacy_provider_seeded", base_url=settings.ai_legacy_base_url)
