"""answer_project_question: reads and appends to a ProjectConversation,
running the tool-calling loop in between. Mirrors summarize_object's skip /
success shape, but unlike that handler this one does touch Mongo directly
(the conversation, and each tool call inside the loop) since a chat answer
genuinely needs project data a payload alone cannot carry.
"""

import asyncio

import pytest
from beanie import PydanticObjectId

from app.errors import PermanentError
from app.models.ai import FailureReason, ProviderKind
from app.models.base import utcnow
from app.models.conversation import ConversationTurn, ProjectConversation
from app.queue import qa_handlers
from app.queue.qa_handlers import answer_project_question
from app.queue.registry import JobContext
from app.services.ai.adapters import Completion, Failure
from app.services.ai.router import ResolvedProvider

pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


def _ctx(payload: dict, owner: str = "local") -> JobContext:
    return JobContext(job_id="job-1", payload=payload, epoch=1, attempts=1, owner=owner)


def _fake_provider():
    return ResolvedProvider(
        provider_id="000000000000000000000000",
        name="Test",
        kind=ProviderKind.OPENAI_COMPAT,
        base_url="http://x:1",
        api_key=None,
        model="test-model",
        models_cache=[],
    )


async def _in_thread(fn, *args, **kwargs):
    """Run `fn` in a real worker thread, matching a THREAD-mode handler's
    actual execution context -- `run_from_thread` inside it schedules onto
    the connect-time loop via `run_coroutine_threadsafe`, which only makes
    sense (and only avoids "this event loop is already running") when the
    caller genuinely is not on that loop's own thread.

    Must itself be awaited from the test coroutine rather than called
    synchronously: blocking the test's own thread on the worker thread's
    result would starve the event loop the worker is trying to schedule
    onto, deadlocking both sides. asyncio.to_thread keeps the loop free to
    service the scheduled coroutine while this awaits the worker.
    """
    return await asyncio.to_thread(fn, *args, **kwargs)


@pytest.fixture(autouse=True)
def real_thread_bridge(monkeypatch):
    """Point qa_handlers.run_from_thread at the real bridge (app.db.client's),
    which requires connect_to_mongo() to have registered a loop -- done here
    against the beanie_models fixture's own loop, since that is the loop
    Motor's client is bound to in this test process."""
    import app.db.client as db_client

    db_client._loop = asyncio.get_event_loop()
    monkeypatch.setattr(qa_handlers, "run_from_thread", db_client.run_from_thread)


async def _make_conversation(owner: str = "local", project_id=None) -> ProjectConversation:
    convo = ProjectConversation(owner=owner, project_id=project_id or PydanticObjectId())
    await convo.insert()
    return convo


class TestPayloadValidation:
    async def test_missing_fields_raise_permanent_error(self):
        with pytest.raises(PermanentError):
            answer_project_question(_ctx({}))

    async def test_missing_conversation_id_raises_permanent_error(self):
        with pytest.raises(PermanentError):
            answer_project_question(
                _ctx({"project_id": str(PydanticObjectId()), "question": "q"})
            )


class TestNoProvider:
    async def test_is_a_skip(self, monkeypatch):
        monkeypatch.setattr(qa_handlers, "_resolve_sync", lambda: None)
        convo = await _make_conversation()

        result = await _in_thread(
            answer_project_question,
            _ctx(
                {
                    "project_id": str(convo.project_id),
                    "question": "how many files?",
                    "conversation_id": str(convo.id),
                }
            ),
        )

        assert result["skipped"] == "no_provider"


class TestFailure:
    async def test_does_not_append_a_half_turn(self, monkeypatch):
        monkeypatch.setattr(qa_handlers, "_resolve_sync", lambda: _fake_provider())
        monkeypatch.setattr(
            qa_handlers.qa, "answer", lambda **k: Failure(FailureReason.UNREACHABLE)
        )
        convo = await _make_conversation()

        result = await _in_thread(
            answer_project_question,
            _ctx(
                {
                    "project_id": str(convo.project_id),
                    "question": "q",
                    "conversation_id": str(convo.id),
                }
            ),
        )

        assert result["skipped"]
        saved = await ProjectConversation.get(convo.id)
        assert saved.turns == []


class TestSuccess:
    async def test_appends_both_turns_and_returns_the_answer(self, monkeypatch):
        monkeypatch.setattr(qa_handlers, "_resolve_sync", lambda: _fake_provider())
        monkeypatch.setattr(qa_handlers.qa, "answer", lambda **k: Completion("42 files", "m"))
        convo = await _make_conversation()

        result = await _in_thread(
            answer_project_question,
            _ctx(
                {
                    "project_id": str(convo.project_id),
                    "question": "how many files?",
                    "conversation_id": str(convo.id),
                }
            ),
        )

        assert result["answer"] == "42 files"
        saved = await ProjectConversation.get(convo.id)
        assert [t.content for t in saved.turns] == ["how many files?", "42 files"]
        assert [t.role for t in saved.turns] == ["user", "assistant"]

    async def test_passes_prior_turns_to_the_loop(self, monkeypatch):
        monkeypatch.setattr(qa_handlers, "_resolve_sync", lambda: _fake_provider())
        seen = {}

        def fake_answer(**kwargs):
            seen.update(kwargs)
            return Completion("ok", "m")

        monkeypatch.setattr(qa_handlers.qa, "answer", fake_answer)

        convo = await _make_conversation()
        convo.turns = [
            ConversationTurn(role="user", content="earlier q", created_at=utcnow()),
            ConversationTurn(role="assistant", content="earlier a", created_at=utcnow()),
        ]
        await convo.save()

        await _in_thread(
            answer_project_question,
            _ctx(
                {
                    "project_id": str(convo.project_id),
                    "question": "new q",
                    "conversation_id": str(convo.id),
                }
            ),
        )

        assert [t.content for t in seen["prior_turns"]] == ["earlier q", "earlier a"]

    async def test_owner_is_passed_through_from_the_context(self, monkeypatch):
        monkeypatch.setattr(qa_handlers, "_resolve_sync", lambda: _fake_provider())
        seen = {}

        def fake_answer(**kwargs):
            seen.update(kwargs)
            return Completion("ok", "m")

        monkeypatch.setattr(qa_handlers.qa, "answer", fake_answer)
        convo = await _make_conversation(owner="specific-owner")

        await _in_thread(
            answer_project_question,
            _ctx(
                {
                    "project_id": str(convo.project_id),
                    "question": "q",
                    "conversation_id": str(convo.id),
                },
                owner="specific-owner",
            ),
        )

        assert seen["owner"] == "specific-owner"

    async def test_a_missing_conversation_document_is_a_permanent_error(self, monkeypatch):
        monkeypatch.setattr(qa_handlers, "_resolve_sync", lambda: _fake_provider())

        with pytest.raises(PermanentError):
            await _in_thread(
                answer_project_question,
                _ctx(
                    {
                        "project_id": str(PydanticObjectId()),
                        "question": "q",
                        "conversation_id": str(PydanticObjectId()),
                    }
                ),
            )
