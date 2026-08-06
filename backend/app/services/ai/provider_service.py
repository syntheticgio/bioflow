"""CRUD over configured providers, and the one action that tests them.

`fetch_models` is deliberately both the connection test and the model
discovery: hitting `/v1/models` proves the base URL resolves, proves the key is
accepted, and returns the dropdown's contents in one round trip. Two separate
concepts would mean two requests proving the same thing.
"""

import asyncio

from bson import ObjectId
from bson.errors import InvalidId

from app.logging import get_logger
from app.models.ai import AiProvider, AiRouting, FailureReason
from app.services.ai import crypto
from app.services.ai.adapters import Failure, adapter_for

log = get_logger(__name__)


def _oid(provider_id: str) -> ObjectId | None:
    try:
        return ObjectId(provider_id)
    except (InvalidId, TypeError):
        return None


async def list_all() -> list[AiProvider]:
    return await AiProvider.find_all().sort("+name").to_list()


async def get(provider_id: str) -> AiProvider | None:
    oid = _oid(provider_id)
    return await AiProvider.get(oid) if oid else None


async def create(
    *, name: str, kind: str, base_url: str, model: str, api_key: str | None
) -> AiProvider:
    provider = AiProvider(name=name, kind=kind, base_url=base_url, model=model)
    if api_key:
        provider.api_key_enc = crypto.encrypt(api_key)
        provider.key_hint = crypto.hint(api_key)
    await provider.insert()
    log.info("ai_provider_created", name=name, kind=str(kind))
    return provider


async def update(provider_id: str, changes: dict) -> AiProvider | None:
    """Apply `changes`. The `api_key` key has three-way semantics.

    Absent from `changes` preserves the stored key; present-and-None clears it;
    present-and-a-string replaces it. This is what lets the UI render a
    write-only key field: the form submits without `api_key` unless the user
    typed one, so editing the model cannot wipe the credential.
    """
    provider = await get(provider_id)
    if provider is None:
        return None

    if "api_key" in changes:
        api_key = changes.pop("api_key")
        if api_key:
            provider.api_key_enc = crypto.encrypt(api_key)
            provider.key_hint = crypto.hint(api_key)
        else:
            provider.api_key_enc = None
            provider.key_hint = None

    for field in ("name", "kind", "base_url", "model"):
        if field in changes:
            setattr(provider, field, changes[field])

    provider.touch()
    await provider.save()
    return provider


async def delete(provider_id: str) -> bool:
    """Delete, clearing any routing that pointed here.

    Clearing rather than refusing: a delete blocked by "three slots use this"
    makes the user go undo three things first, and the slots fall back to the
    default perfectly well on their own.
    """
    provider = await get(provider_id)
    if provider is None:
        return False

    routing = await AiRouting.load()
    dirty = False
    if routing.default == provider_id:
        routing.default = None
        dirty = True
    for slot, assigned in list(routing.slots.items()):
        if assigned == provider_id:
            del routing.slots[slot]
            dirty = True
    if dirty:
        await routing.save()

    await provider.delete()
    log.info("ai_provider_deleted", name=provider.name)
    return True


def _list_models(provider: AiProvider) -> list[str] | Failure:
    """The blocking call, factored out so tests can replace it.

    Separate from `fetch_models` because that function's job -- persisting the
    result -- is what the tests are about, and stubbing a socket to test a
    database write is the wrong seam.
    """
    key = crypto.decrypt(provider.api_key_enc) if provider.api_key_enc else None
    adapter = adapter_for(provider.kind, base_url=provider.base_url, api_key=key)
    return adapter.list_models()


def _list_models_with_context(provider: AiProvider) -> dict[str, int | None] | Failure:
    """Same blocking-call seam as `_list_models`, for context_length capture."""
    key = crypto.decrypt(provider.api_key_enc) if provider.api_key_enc else None
    adapter = adapter_for(provider.kind, base_url=provider.base_url, api_key=key)
    return adapter.list_models_with_context()


async def fetch_models(provider_id: str) -> list[str] | Failure | None:
    """Fetch and cache the model list. Returns None if the provider is gone.

    Doubles as the connection test: on success the provider is marked ok, on
    failure it carries the reason, and either way `checked_at` moves.
    """
    provider = await get(provider_id)
    if provider is None:
        return None

    # Off the event loop: urllib blocks, and an unreachable host is slow rather
    # than instant.
    result = await asyncio.to_thread(_list_models, provider)

    if isinstance(result, Failure):
        provider.mark_failed(result.reason)
        # models_cache deliberately untouched -- a bad day at the listing
        # endpoint should not empty the user's model dropdown.
        await provider.save()
        return result

    provider.models_cache = result

    # Best-effort: a context-length fetch failing must not fail the whole
    # model-list refresh, which is the thing the caller actually asked for.
    context_result = await asyncio.to_thread(_list_models_with_context, provider)
    if not isinstance(context_result, Failure):
        provider.context_windows = {
            model_id: length
            for model_id, length in context_result.items()
            if length is not None
        }

    provider.mark_ok()
    await provider.save()
    return result


async def record_failure(provider_id: str, reason: FailureReason) -> None:
    """Mark a provider failed from a real job, not a manual test.

    This is what makes the settings badge reflect usage: a key that expired
    between fetches shows as failed the next time a summary is attempted.
    """
    provider = await get(provider_id)
    if provider is None:
        return
    provider.mark_failed(reason)
    await provider.save()


async def record_success(provider_id: str) -> None:
    provider = await get(provider_id)
    if provider is None:
        return
    provider.mark_ok()
    await provider.save()
