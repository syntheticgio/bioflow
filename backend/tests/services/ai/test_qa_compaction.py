"""Threshold-triggered compaction: fold old turns into a summary once the
live tail threatens the routed provider's context window, without ever
truncating the on-disk transcript.
"""

import pytest
from beanie import PydanticObjectId

from app.config import settings
from app.models.ai import FailureReason
from app.models.base import utcnow
from app.models.conversation import ConversationTurn, ProjectConversation
from app.services.ai import qa_compaction
from app.services.ai.adapters import Completion, Failure

pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


def _turn(content: str, role: str = "user") -> ConversationTurn:
    return ConversationTurn(role=role, content=content, created_at=utcnow())


class TestNeedsCompaction:
    def test_short_conversation_is_under_threshold(self):
        convo = ProjectConversation(
            owner="local", project_id=PydanticObjectId(),
            turns=[_turn("hi"), _turn("hello", role="assistant")],
        )
        assert not qa_compaction.needs_compaction(convo, context_length=8000)

    def test_long_conversation_crosses_threshold(self):
        long_turns = [_turn("x" * 5000) for _ in range(20)]
        convo = ProjectConversation(
            owner="local", project_id=PydanticObjectId(), turns=long_turns
        )
        assert qa_compaction.needs_compaction(convo, context_length=8000)

    def test_turns_already_folded_do_not_count_toward_the_estimate(self):
        """compacted_through marks turns already summarized -- they must not
        be double-counted against the live-tail budget."""
        long_turns = [_turn("x" * 5000) for _ in range(20)]
        convo = ProjectConversation(
            owner="local", project_id=PydanticObjectId(),
            turns=long_turns, compacted_through=20,
        )
        assert not qa_compaction.needs_compaction(convo, context_length=8000)

    def test_none_context_length_falls_back_to_the_configured_default(self):
        big_content = "x" * (settings.qa_default_context_tokens * 4)
        convo = ProjectConversation(
            owner="local", project_id=PydanticObjectId(), turns=[_turn(big_content)]
        )
        assert qa_compaction.needs_compaction(convo, context_length=None)

    def test_none_context_length_with_a_short_conversation_is_not_over_threshold(self):
        convo = ProjectConversation(
            owner="local", project_id=PydanticObjectId(), turns=[_turn("hi")]
        )
        assert not qa_compaction.needs_compaction(convo, context_length=None)


class TestCompact:
    def test_folds_turns_before_compacted_through_and_advances_it(self, monkeypatch):
        monkeypatch.setattr(
            qa_compaction, "complete_sync", lambda *a, **k: Completion("condensed summary", "m")
        )
        convo = ProjectConversation(
            owner="local", project_id=PydanticObjectId(),
            turns=[_turn("q1"), _turn("a1", role="assistant")],
        )

        qa_compaction.compact(convo, provider=object())

        assert convo.compacted_summary == "condensed summary"
        assert convo.compacted_through == 2
        assert len(convo.turns) == 2  # retained on disk, never deleted

    def test_a_second_compaction_includes_the_prior_summary_as_context(self, monkeypatch):
        seen_prompts = []

        def fake_complete(*a, user="", **k):
            seen_prompts.append(user)
            return Completion("new summary", "m")

        monkeypatch.setattr(qa_compaction, "complete_sync", fake_complete)
        convo = ProjectConversation(
            owner="local", project_id=PydanticObjectId(),
            turns=[_turn("q1"), _turn("a1", role="assistant")],
            compacted_summary="old summary",
            compacted_through=0,
        )

        qa_compaction.compact(convo, provider=object())

        assert "old summary" in seen_prompts[0]

    def test_failure_leaves_the_conversation_unchanged(self, monkeypatch):
        monkeypatch.setattr(
            qa_compaction, "complete_sync", lambda *a, **k: Failure(FailureReason.UNREACHABLE)
        )
        convo = ProjectConversation(
            owner="local", project_id=PydanticObjectId(),
            turns=[_turn("q1"), _turn("a1", role="assistant")],
        )
        before = (convo.compacted_summary, convo.compacted_through)

        qa_compaction.compact(convo, provider=object())

        assert (convo.compacted_summary, convo.compacted_through) == before

    def test_no_turns_to_fold_is_a_noop(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            qa_compaction, "complete_sync", lambda *a, **k: called.append(1) or Completion("x", "m")
        )
        convo = ProjectConversation(
            owner="local", project_id=PydanticObjectId(),
            turns=[_turn("q1")], compacted_through=1,
        )

        qa_compaction.compact(convo, provider=object())

        assert not called
