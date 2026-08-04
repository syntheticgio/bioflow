"""Which provider serves a given task.

The one function call sites use. Everything upstream of a `ResolvedProvider` --
the routing document, the fallback to default, the key decryption -- happens
here, so a caller needs to know only its own slot.
"""

from dataclasses import dataclass

from app.logging import get_logger
from app.models.ai import AiRouting, TaskSlot
from app.services.ai import crypto, provider_service

log = get_logger(__name__)


@dataclass(frozen=True)
class ResolvedProvider:
    """A provider with its key decrypted, ready to build an adapter from.

    `provider_id` rides along so a failure can be recorded back onto the
    document it came from -- that write is what makes the settings badge
    reflect real usage rather than only the last manual test.
    """

    provider_id: str
    name: str
    kind: str
    base_url: str
    api_key: str | None
    model: str
    models_cache: list[str]


async def resolve(slot: TaskSlot) -> ResolvedProvider | None:
    """The provider serving `slot`, or None if nothing is configured.

    None is a normal state, not an error: a fresh install has no providers, and
    every caller treats that the same way it treated a model server being off.
    """
    routing = await AiRouting.load()
    provider_id = routing.provider_for(slot)
    if not provider_id:
        return None

    provider = await provider_service.get(provider_id)
    if provider is None:
        # Deleting clears routing, so this means a hand-edited database.
        log.warning("ai_routing_dangling", slot=slot.value, provider_id=provider_id)
        return None

    key = crypto.decrypt(provider.api_key_enc) if provider.api_key_enc else None
    return ResolvedProvider(
        provider_id=str(provider.id),
        name=provider.name,
        kind=provider.kind,
        base_url=provider.base_url,
        api_key=key,
        model=provider.model,
        models_cache=list(provider.models_cache),
    )
