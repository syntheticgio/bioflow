"""The DE narrative-summary job. Mirrors summary_handlers.py exactly, pointed
at DE_SUMMARY instead of FILE_SUMMARY and de_summary_prompt instead of
summary_prompt. See summary_handlers.py's module docstring for why this is
THREAD mode and why the whole job is best-effort.
"""

import importlib

from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.queue.registry import HandlerMode, JobContext, handler
from app.services import de_summary_prompt
from app.services.ai.adapters import Completion

# See summary_handlers.py for why this goes through importlib rather than a
# normal import: app.services.ai's __init__ shadows the `complete` submodule
# with the re-exported function of the same name.
ai_complete = importlib.import_module("app.services.ai.complete")

log = get_logger(__name__)


def _resolve_sync():
    from app.db.client import run_from_thread
    from app.models.ai import TaskSlot
    from app.services.ai import router

    return run_from_thread(router.resolve(TaskSlot.DE_SUMMARY))


def _complete(provider, **kwargs):
    return ai_complete.complete_sync(provider, **kwargs)


@handler(
    "summarize_de_results",
    mode=HandlerMode.THREAD,
    job_class=JobClass.USER_BACKGROUND,
    resources=JobResources(cpu=0, mem_mb=64, io=IoClass.LIGHT),
    max_attempts=2,
)
def summarize_de_results(ctx: JobContext) -> dict:
    """Generate a short narrative summary of a differential-expression run.

    Receives everything it needs in the payload, same reasoning as
    summarize_object: this handler runs in a thread and cannot reach the
    database. The caller assembles the payload on the event loop; see
    pipeline_service.launch_de_summary.
    """
    object_id = ctx.payload.get("object_id")
    if not object_id:
        from app.errors import PermanentError

        raise PermanentError("summarize_de_results requires an 'object_id'")

    ctx.check_cancel()

    provider = _resolve_sync()
    if provider is None:
        log.info("de_summary_skipped_no_provider", object_id=object_id)
        return {"object_id": object_id, "skipped": "no_provider"}

    prompt = de_summary_prompt.build_de_user_prompt(
        facts=ctx.payload.get("facts") or {},
        top_genes=ctx.payload.get("top_genes") or [],
    )
    if prompt is None:
        log.info("de_summary_skipped_insufficient_data", object_id=object_id)
        return {"object_id": object_id, "skipped": "insufficient_data"}

    ctx.check_cancel()
    ctx.extend_lease(int(_timeout_seconds()) + 60)

    result = _complete(
        provider, system=de_summary_prompt.DE_SYSTEM_PROMPT, user=prompt
    )
    if not isinstance(result, Completion):
        log.info("de_summary_not_generated", object_id=object_id, reason=result.reason)
        return {"object_id": object_id, "skipped": str(result.reason)}

    text, model = result.text, result.model
    log.info("de_summary_generated", object_id=object_id, model=model, chars=len(text))
    return {
        "object_id": object_id,
        "summary": text,
        "model": model,
        "facts_fingerprint": ctx.payload.get("facts_fingerprint"),
    }


def _timeout_seconds() -> float:
    from app.config import settings

    return settings.llm_timeout_seconds
