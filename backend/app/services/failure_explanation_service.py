"""A plain-language explanation of a job error, generated once and cached.

Read-through cache: a hit is a single indexed document read, and a miss
calls the model and stores the result. Like the organism blurb and unlike
the file summary, this does not go through the job queue -- the explanation
is wanted the instant a user clicks "Explain this error," it takes one short
generation, and a queued job would mean the panel shows an empty state that
pops in seconds later. A synchronous call the UI can show a loading state
for is the honest presentation of that.

Every failure yields None. The explanation is a plain-language restatement
of an error the user can already see verbatim; a model that is not running,
or one that produces nothing, simply means no restatement appears, exactly
as before this existed.
"""

import importlib

from app.logging import get_logger
from app.models import FailureExplanation, normalize_failure
from app.services import failure_explanation_prompt
from app.services.ai import router as ai_router
from app.services.ai.adapters import Completion

# NOT `from app.services.ai import complete as ai_complete`: see
# organism_service.py's identical comment for why this goes through
# importlib rather than a normal import.
ai_complete = importlib.import_module("app.services.ai.complete")

log = get_logger(__name__)


async def get_cached(code: str, message: str) -> FailureExplanation | None:
    """The stored explanation for an error, if one has been written."""
    return await FailureExplanation.find_one(
        FailureExplanation.failure_key == normalize_failure(code, message)
    )


async def get_or_generate(code: str, message: str) -> FailureExplanation | None:
    """The explanation for an error, generating and caching it on a miss."""
    from app.config import settings

    if not settings.llm_summaries_enabled:
        return None

    key = normalize_failure(code, message)
    cached = await FailureExplanation.find_one(FailureExplanation.failure_key == key)
    if cached is not None:
        return cached

    from app.models.ai import TaskSlot

    provider = await ai_router.resolve(TaskSlot.FAILURE_EXPLANATION)
    if provider is None:
        return None

    result = await ai_complete.complete(
        provider,
        system=failure_explanation_prompt.FAILURE_SYSTEM_PROMPT,
        user=failure_explanation_prompt.build_failure_prompt(code, message),
        # Shorter than a file summary: this is one to three sentences, and
        # the cap is what stops a chatty model from writing an essay.
        max_tokens=200,
    )
    if not isinstance(result, Completion):
        return None

    text, model = result.text, result.model
    log.info("failure_explanation_generated", key=key, model=model, chars=len(text))

    # Upsert rather than insert: two jobs failing with the same error can
    # reach here concurrently, and the unique index would turn the loser's
    # insert into an error over an explanation that is already correct.
    await FailureExplanation.find_one(FailureExplanation.failure_key == key).upsert(
        {
            "$set": {
                "code": code,
                "message": message,
                "text": text,
                "model": model,
                "generated_at": _now(),
                "updated_at": _now(),
            }
        },
        on_insert=FailureExplanation(
            failure_key=key,
            code=code,
            message=message,
            text=text,
            model=model,
        ),
    )

    return await FailureExplanation.find_one(FailureExplanation.failure_key == key)


def _now():
    from app.models.base import utcnow

    return utcnow()
