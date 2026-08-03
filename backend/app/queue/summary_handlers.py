"""The narrative-summary job.

Its own module rather than a function in `pipeline_handlers.py` because it is
not a pipeline: it spawns no subprocess, touches no blob bytes and produces no
file. All it does is turn numbers this app already has into a few sentences.

THREAD mode, not ASYNC: the model call is a blocking HTTP request that can sit
for a minute or more on a small local model, and blocking the event loop for
that long would stall every heartbeat in the worker.

The whole job is best-effort by design. A model server that is not running is
the expected steady state for anyone who has not started one, so "no server"
returns an empty result and succeeds rather than failing the job and filling the
activity view with red rows for a feature the user may not have opted into.
"""

import importlib

from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.queue.registry import HandlerMode, JobContext, handler
from app.services import summary_prompt
from app.services.ai.adapters import Completion

# NOT `from app.services.ai import complete as ai_complete`, and NOT
# `import app.services.ai.complete as ai_complete` either: app/services/ai/
# __init__.py does `from app.services.ai.complete import complete`, which
# rebinds the *package attribute* `complete` to the function it re-exports,
# shadowing the submodule of the same name. Both of those import forms
# resolve through that attribute and would silently bind the function, not
# the module -- so this goes through `sys.modules` via `importlib` instead,
# which is the only form immune to the shadow, and gives tests a module to
# monkeypatch `.complete_sync` on.
ai_complete = importlib.import_module("app.services.ai.complete")

log = get_logger(__name__)


def _resolve_sync():
    """Resolve the FILE_SUMMARY provider from a worker thread.

    Thread handlers have no event loop, and `router.resolve` is async because
    it reads Mongo. `asyncio.run` on a fresh loop is the standard escape here
    and is cheap next to the model call that follows.
    """
    import asyncio

    from app.models.ai import TaskSlot
    from app.services.ai import router

    return asyncio.run(router.resolve(TaskSlot.FILE_SUMMARY))


def _complete(provider, **kwargs):
    """The model call. Separate so tests replace it without a socket."""
    return ai_complete.complete_sync(provider, **kwargs)


@handler(
    "summarize_object",
    mode=HandlerMode.THREAD,
    # USER_BACKGROUND, not COMPUTE: the work happens in another process on the
    # host, so this job holds no CPU of its own and must not compete with a
    # trim or an alignment for the compute budget.
    job_class=JobClass.USER_BACKGROUND,
    resources=JobResources(cpu=0, mem_mb=64, io=IoClass.LIGHT),
    # One retry. A summary is a nicety, and the usual reason for failure -- the
    # server is not running -- will not be fixed by trying again in a minute.
    max_attempts=2,
)
def summarize_object(ctx: JobContext) -> dict:
    """Generate a short narrative summary of one file's QC data and metadata.

    Receives everything it needs in the payload -- facts, metadata, organism --
    because thread handlers cannot reach the database. The caller assembles the
    payload on the event loop; see `pipeline_service.launch_summary`.
    """
    object_id = ctx.payload.get("object_id")
    if not object_id:
        # A permanent shape error in the payload, unlike the soft failures
        # below: no amount of retrying fixes a job that does not say what it
        # is about.
        from app.errors import PermanentError

        raise PermanentError("summarize_object requires an 'object_id'")

    ctx.check_cancel()

    provider = _resolve_sync()
    if provider is None:
        log.info("summary_skipped_no_provider", object_id=object_id)
        return {"object_id": object_id, "skipped": "no_provider"}

    prompt = summary_prompt.build_user_prompt(
        name=ctx.payload.get("name") or "unnamed file",
        format_kind=ctx.payload.get("format_kind") or "unknown",
        role=ctx.payload.get("role"),
        organism=ctx.payload.get("organism"),
        facts=ctx.payload.get("facts") or {},
        metadata=ctx.payload.get("metadata") or {},
    )
    if prompt is None:
        log.info("summary_skipped_insufficient_data", object_id=object_id)
        return {"object_id": object_id, "skipped": "insufficient_data"}

    ctx.check_cancel()

    # The model call is the slow part and the lease has to survive it. Asked for
    # explicitly with headroom over the client timeout, so a slow generation
    # cannot get this job reaped and re-run underneath itself.
    ctx.extend_lease(int(_timeout_seconds()) + 60)

    result = _complete(provider, system=summary_prompt.SYSTEM_PROMPT, user=prompt)
    if not isinstance(result, Completion):
        # A typed reason rather than a bare "nothing happened": an expired key
        # is a configuration problem the user can fix, and silence hides it.
        log.info("summary_not_generated", object_id=object_id, reason=result.reason)
        return {"object_id": object_id, "skipped": str(result.reason)}

    text, model = result.text, result.model
    log.info("summary_generated", object_id=object_id, model=model, chars=len(text))
    return {
        "object_id": object_id,
        "summary": text,
        "model": model,
        # The digest of what was summarized, so the UI can tell a current
        # summary from one written before the last QC run. See
        # `results._apply_summarize_object`.
        "facts_fingerprint": ctx.payload.get("facts_fingerprint"),
    }


def _timeout_seconds() -> float:
    from app.config import settings

    return settings.llm_timeout_seconds
