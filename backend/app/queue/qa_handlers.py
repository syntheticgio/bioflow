"""The project Q&A job: reads and appends to a ProjectConversation, running
the tool-calling loop (app/services/ai/qa.py) in between.

THREAD mode, like summarize_object -- the model call and the tool-call round
trips inside it are blocking, and can run long enough to stall the event
loop's heartbeat if not run off it.

Unlike summarize_object, this handler does touch Mongo directly (the
conversation document, and each tool call the loop makes) rather than
receiving everything through the payload. A chat answer genuinely needs live
project data mid-loop that cannot be assembled ahead of time the way a
summary's facts/metadata snapshot can. Every such access goes through
`run_from_thread`, bridging onto the connect-time event loop -- see
summary_handlers._resolve_sync's docstring for why `asyncio.run()` would be
wrong here.

max_attempts=1, not summarize_object's 2: a retried Q&A answer after a
partial tool-loop failure risks asking the model to re-derive context it may
answer differently the second time, and a conversation's turns are mutated by
even a failed attempt reaching the compaction step -- a naive retry replays
against changed state rather than a clean slate.
"""

from beanie import PydanticObjectId

from app.errors import PermanentError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.models.base import utcnow
from app.models.conversation import ConversationTurn as StoredTurn
from app.models.conversation import ProjectConversation
from app.queue.registry import HandlerMode, JobContext, handler
from app.services.ai import qa, qa_compaction
from app.services.ai.adapters import Completion
from app.services.ai.adapters import ConversationTurn as LoopTurn

log = get_logger(__name__)


def _resolve_sync():
    """Resolve the PROJECT_QA provider from a worker thread. See
    summary_handlers._resolve_sync for the full rationale."""
    from app.db.client import run_from_thread as _bridge
    from app.models.ai import TaskSlot
    from app.services.ai import router

    return _bridge(router.resolve(TaskSlot.PROJECT_QA))


def run_from_thread(coro):
    """Bridge one Mongo-touching coroutine onto the connect-time loop.
    Its own function so tests have a seam that is not a real second loop."""
    from app.db.client import run_from_thread as _bridge

    return _bridge(coro)


def _context_length_for(provider) -> int | None:
    async def _load():
        from app.models.ai import AiProvider

        doc = await AiProvider.get(PydanticObjectId(provider.provider_id))
        if doc is None:
            return None
        return doc.context_windows.get(provider.model)

    return run_from_thread(_load())


@handler(
    "answer_project_question",
    mode=HandlerMode.THREAD,
    # USER_INTERACTIVE, not USER_BACKGROUND: a chat message is something the
    # user is actively waiting on inside an open drawer, closer in urgency to
    # an interactive click than to a background summarization pass.
    job_class=JobClass.USER_INTERACTIVE,
    resources=JobResources(cpu=0, mem_mb=64, io=IoClass.LIGHT),
    max_attempts=1,
)
def answer_project_question(ctx: JobContext) -> dict:
    project_id = ctx.payload.get("project_id")
    question = ctx.payload.get("question")
    conversation_id = ctx.payload.get("conversation_id")
    if not project_id or not question or not conversation_id:
        raise PermanentError(
            "answer_project_question requires project_id, question, conversation_id"
        )

    ctx.check_cancel()

    provider = _resolve_sync()
    if provider is None:
        log.info("qa_skipped_no_provider", conversation_id=conversation_id)
        return {"conversation_id": conversation_id, "skipped": "no_provider"}

    convo = run_from_thread(ProjectConversation.get(PydanticObjectId(conversation_id)))
    if convo is None:
        raise PermanentError(f"no ProjectConversation with id {conversation_id}")

    context_length = _context_length_for(provider)
    if qa_compaction.needs_compaction(convo, context_length=context_length):
        qa_compaction.compact(convo, provider=provider)
        run_from_thread(convo.save())

    prior_turns = [
        LoopTurn(role=t.role, content=t.content)
        for t in convo.turns[convo.compacted_through :]
    ]

    ctx.check_cancel()
    ctx.extend_lease(180)

    result = qa.answer(
        provider=provider,
        question=question,
        project_id=PydanticObjectId(project_id),
        owner=ctx.owner,
        prior_turns=prior_turns,
    )

    if not isinstance(result, Completion):
        reason = getattr(result, "reason", result)
        log.info("qa_not_answered", conversation_id=conversation_id, reason=reason)
        return {"conversation_id": conversation_id, "project_id": project_id, "skipped": str(reason)}

    now = utcnow()
    convo.turns.append(StoredTurn(role="user", content=question, created_at=now))
    convo.turns.append(StoredTurn(role="assistant", content=result.text, created_at=now))
    run_from_thread(convo.save())

    return {"conversation_id": conversation_id, "project_id": project_id, "answer": result.text}
