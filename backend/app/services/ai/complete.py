"""One chat completion against a resolved provider.

Where the package's two rules meet: **never raise**, and **leave a trace**.
The first is inherited from the `llm_client` module this replaces -- summaries
are additive, so a failure must not fail a job. The second is new, because a
hosted provider fails for reasons the user can fix (an expired key, an
exhausted quota) and silence hides them.
"""

import asyncio

from app.config import settings
from app.logging import get_logger
from app.models.ai import FailureReason
from app.services.ai import provider_service, redaction
from app.services.ai.adapters import (
    Completion,
    ConversationTurn,
    Failure,
    ToolCall,
    ToolSpec,
    adapter_for,
)
from app.services.ai.router import ResolvedProvider

log = get_logger(__name__)


def _run(provider: ResolvedProvider, **kwargs) -> Completion | ToolCall | Failure:
    """The blocking adapter call. Its own function so tests have a seam that
    is not a socket."""
    adapter = adapter_for(
        provider.kind, base_url=provider.base_url, api_key=provider.api_key
    )
    return adapter.complete(**kwargs)


def _model_for(provider: ResolvedProvider) -> str | None:
    """The model to send.

    Falls back to the first cached model when none is pinned: a provider added
    and fetched but never given an explicit model is one click from working,
    and picking for the user beats refusing.
    """
    if provider.model:
        return provider.model
    return provider.models_cache[0] if provider.models_cache else None


async def complete(
    provider: ResolvedProvider,
    *,
    system: str,
    user: str,
    max_tokens: int | None = None,
    tools: list[ToolSpec] | None = None,
    history: list[ConversationTurn] | None = None,
) -> Completion | ToolCall | Failure:
    """Run one completion, recording the outcome on the provider document.

    A `ToolCall` result counts as success for recording purposes -- the
    round trip to the provider worked, even though no final text came back
    yet.
    """
    model = _model_for(provider)
    if model is None:
        log.info("ai_no_model", provider=provider.name)
        await provider_service.record_failure(
            provider.provider_id, FailureReason.MODEL_NOT_FOUND
        )
        return Failure(FailureReason.MODEL_NOT_FOUND)

    try:
        result = await asyncio.to_thread(
            _run,
            provider,
            system=system,
            user=user,
            model=model,
            max_tokens=max_tokens or settings.llm_max_tokens,
            tools=tools,
            history=history,
        )
    except Exception as e:  # noqa: BLE001 - the invariant: never raise into a job
        # Scrub before truncating, not after: slicing first can split the key
        # across the boundary, leaving a prefix that `replace` no longer
        # matches. `scrub` truncates to MAX_BODY_CHARS itself. The same string
        # goes to the log and to the Failure, which `record_failure` stores and
        # the settings page renders -- both need the key gone.
        detail = redaction.scrub(str(e), provider.api_key)
        log.warning("ai_call_crashed", provider=provider.name, error=detail)
        result = Failure(FailureReason.BAD_RESPONSE, detail)

    if isinstance(result, Failure):
        await provider_service.record_failure(provider.provider_id, result.reason)
        return result

    await provider_service.record_success(provider.provider_id)
    return result


def complete_sync(
    provider: ResolvedProvider,
    *,
    system: str,
    user: str,
    max_tokens: int | None = None,
    tools: list[ToolSpec] | None = None,
    history: list[ConversationTurn] | None = None,
) -> Completion | ToolCall | Failure:
    """Blocking variant for thread handlers, which cannot await.

    Queue handlers run in a worker thread with no event loop, so they cannot
    reach the async version. They get no failure recording for the same reason
    -- the write needs the loop -- and the handler returns the reason in its
    result payload instead, where `results.py` persists it.
    """
    model = _model_for(provider)
    if model is None:
        return Failure(FailureReason.MODEL_NOT_FOUND)
    try:
        return _run(
            provider,
            system=system,
            user=user,
            model=model,
            max_tokens=max_tokens or settings.llm_max_tokens,
            tools=tools,
            history=history,
        )
    except Exception as e:  # noqa: BLE001
        # See the note in complete(): scrub before truncating. Here the
        # Failure detail reaches the handler's result payload, which
        # `results.py` persists.
        detail = redaction.scrub(str(e), provider.api_key)
        log.warning("ai_call_crashed", provider=provider.name, error=detail)
        return Failure(FailureReason.BAD_RESPONSE, detail)
