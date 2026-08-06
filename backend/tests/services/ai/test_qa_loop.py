"""The bounded tool-calling loop: up to 3 tool calls, then a forced final
answer if the model still hasn't settled on one.
"""

from unittest.mock import AsyncMock

import pytest
from beanie import PydanticObjectId

from app.models.ai import FailureReason
from app.services.ai import qa, qa_tools
from app.services.ai.adapters import Completion, ConversationTurn, Failure, ToolCall


@pytest.fixture(autouse=True)
def no_thread_bridge(monkeypatch):
    """qa.answer() runs synchronously (called from a THREAD handler) but the
    tool executors are async coroutines; run_from_thread bridges that in
    production via a persistent loop. In tests there is no such loop, so a
    fresh one is created and torn down per call -- get_event_loop() is not
    reliable here since other async tests in this suite close their own
    loops, leaving none "current" in a sync context."""
    import asyncio

    def run_now(coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    monkeypatch.setattr(qa, "run_from_thread", run_now)


PROJECT_ID = PydanticObjectId()
OWNER = "qa-loop-owner"


class TestNoToolCallNeeded:
    def test_returns_immediately_when_the_model_answers_directly(self, monkeypatch):
        monkeypatch.setattr(qa, "complete_sync", lambda *a, **k: Completion("42", "m"))

        result = qa.answer(provider=object(), question="how many files?", project_id=PROJECT_ID, owner=OWNER)

        assert isinstance(result, Completion)
        assert result.text == "42"


class TestOneToolCall:
    def test_executes_the_tool_and_feeds_the_result_back(self, monkeypatch):
        calls = []

        def fake_complete(*a, **k):
            calls.append(k)
            if len(calls) == 1:
                return ToolCall(id="1", name="search_objects", arguments={"kinds": ["bam"]})
            return Completion("there are 3 bams", "m")

        monkeypatch.setattr(qa, "complete_sync", fake_complete)
        monkeypatch.setattr(
            qa_tools, "execute_search_objects", AsyncMock(return_value={"objects": [], "total": 3})
        )

        result = qa.answer(provider=object(), question="how many bams?", project_id=PROJECT_ID, owner=OWNER)

        assert isinstance(result, Completion)
        assert result.text == "there are 3 bams"
        assert len(calls) == 2
        second_history = calls[1]["history"]
        assert any(t.role == "tool_result" for t in second_history)
        assert any(t.role == "tool_call" for t in second_history)

    def test_the_tool_result_is_scoped_with_project_id_and_owner(self, monkeypatch):
        seen = {}

        def fake_complete(*a, **k):
            if "search" not in seen:
                seen["search"] = True
                return ToolCall(id="1", name="search_objects", arguments={})
            return Completion("done", "m")

        monkeypatch.setattr(qa, "complete_sync", fake_complete)
        mock_execute = AsyncMock(return_value={"objects": []})
        monkeypatch.setattr(qa_tools, "execute_search_objects", mock_execute)

        qa.answer(provider=object(), question="q", project_id=PROJECT_ID, owner=OWNER)

        mock_execute.assert_awaited_once_with({}, project_id=PROJECT_ID, owner=OWNER)

    def test_list_jobs_dispatches_correctly(self, monkeypatch):
        calls = []

        def fake_complete(*a, **k):
            calls.append(k)
            if len(calls) == 1:
                return ToolCall(id="1", name="list_jobs", arguments={"state": "running"})
            return Completion("one job running", "m")

        monkeypatch.setattr(qa, "complete_sync", fake_complete)
        mock_execute = AsyncMock(return_value={"jobs": []})
        monkeypatch.setattr(qa_tools, "execute_list_jobs", mock_execute)

        result = qa.answer(provider=object(), question="anything running?", project_id=PROJECT_ID, owner=OWNER)

        assert isinstance(result, Completion)
        mock_execute.assert_awaited_once_with({"state": "running"}, project_id=PROJECT_ID, owner=OWNER)


class TestLoopCap:
    def test_stops_after_three_tool_calls_and_forces_a_final_answer(self, monkeypatch):
        call_count = 0

        def fake_complete(*a, tools=None, **k):
            nonlocal call_count
            call_count += 1
            if tools is None:
                return Completion("best guess given what I found", "m")
            return ToolCall(id=str(call_count), name="search_objects", arguments={})

        monkeypatch.setattr(qa, "complete_sync", fake_complete)
        monkeypatch.setattr(qa_tools, "execute_search_objects", AsyncMock(return_value={}))

        result = qa.answer(provider=object(), question="q", project_id=PROJECT_ID, owner=OWNER)

        assert isinstance(result, Completion)
        assert result.text == "best guess given what I found"
        assert call_count == qa.MAX_TOOL_CALLS + 1

    def test_the_forced_final_call_passes_no_tools(self, monkeypatch):
        seen_tools = []

        def fake_complete(*a, tools=None, **k):
            seen_tools.append(tools)
            if tools is None:
                return Completion("final", "m")
            return ToolCall(id="x", name="search_objects", arguments={})

        monkeypatch.setattr(qa, "complete_sync", fake_complete)
        monkeypatch.setattr(qa_tools, "execute_search_objects", AsyncMock(return_value={}))

        qa.answer(provider=object(), question="q", project_id=PROJECT_ID, owner=OWNER)

        assert seen_tools[-1] is None


class TestFailure:
    def test_a_failure_aborts_immediately(self, monkeypatch):
        monkeypatch.setattr(qa, "complete_sync", lambda *a, **k: Failure(FailureReason.UNREACHABLE))

        result = qa.answer(provider=object(), question="q", project_id=PROJECT_ID, owner=OWNER)

        assert isinstance(result, Failure)
        assert result.reason is FailureReason.UNREACHABLE

    def test_a_failure_mid_loop_does_not_run_the_forced_final_call(self, monkeypatch):
        calls = []

        def fake_complete(*a, **k):
            calls.append(1)
            if len(calls) == 1:
                return ToolCall(id="1", name="search_objects", arguments={})
            return Failure(FailureReason.UNREACHABLE)

        monkeypatch.setattr(qa, "complete_sync", fake_complete)
        monkeypatch.setattr(qa_tools, "execute_search_objects", AsyncMock(return_value={}))

        result = qa.answer(provider=object(), question="q", project_id=PROJECT_ID, owner=OWNER)

        assert isinstance(result, Failure)
        assert len(calls) == 2


class TestUnknownTool:
    def test_unknown_tool_name_is_a_bad_response_not_a_crash(self, monkeypatch):
        monkeypatch.setattr(
            qa, "complete_sync", lambda *a, **k: ToolCall(id="1", name="delete_everything", arguments={})
        )

        result = qa.answer(provider=object(), question="q", project_id=PROJECT_ID, owner=OWNER)

        assert isinstance(result, Failure)
        assert result.reason is FailureReason.BAD_RESPONSE


class TestPriorTurns:
    def test_prior_turns_are_prepended_to_the_new_question(self, monkeypatch):
        seen_history = []

        def fake_complete(*a, history=None, **k):
            seen_history.append(history)
            return Completion("ok", "m")

        monkeypatch.setattr(qa, "complete_sync", fake_complete)
        prior = [
            ConversationTurn(role="user", content="q1"),
            ConversationTurn(role="assistant", content="a1"),
        ]

        qa.answer(provider=object(), question="q2", project_id=PROJECT_ID, owner=OWNER, prior_turns=prior)

        history = seen_history[0]
        assert history[0].content == "q1"
        assert history[1].content == "a1"
        assert history[2].content == "q2"
