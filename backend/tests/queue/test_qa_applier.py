"""_apply_answer_project_question: a structural no-op on the data model --
there is no object this job is "about" to write facts onto -- existing only
so the dispatch table's exhaustiveness holds and so a qa.answered event gets
published for the frontend to refetch on.
"""

import pytest

from app.queue import results

pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


class TestDispatchRegistration:
    async def test_dispatch_table_includes_the_new_type(self):
        assert "answer_project_question" in results._APPLIERS
        assert results._APPLIERS["answer_project_question"] is results._apply_answer_project_question


class TestApplier:
    async def test_success_publishes_qa_answered(self, monkeypatch):
        published = []

        async def fake_publish(event_type, data, *, owner):
            published.append((event_type, data, owner))

        monkeypatch.setattr(results, "publish_event", fake_publish)

        await results._apply_answer_project_question(
            {"conversation_id": "abc123", "project_id": "def456", "answer": "42"},
            owner="local",
        )

        assert len(published) == 1
        event_type, data, owner = published[0]
        assert event_type == "qa.answered"
        assert data["conversation_id"] == "abc123"
        assert owner == "local"

    async def test_skip_is_a_noop_publishing_nothing(self, monkeypatch):
        """A skipped job (no provider, or the model failed) means no new turn
        was written -- nothing for the frontend to refetch."""
        published = []

        async def fake_publish(event_type, data, *, owner):
            published.append((event_type, data, owner))

        monkeypatch.setattr(results, "publish_event", fake_publish)

        await results._apply_answer_project_question(
            {"conversation_id": "abc123", "skipped": "no_provider"}, owner="local"
        )

        assert published == []

    async def test_missing_answer_key_entirely_is_also_a_noop(self, monkeypatch):
        published = []

        async def fake_publish(event_type, data, *, owner):
            published.append((event_type, data, owner))

        monkeypatch.setattr(results, "publish_event", fake_publish)

        await results._apply_answer_project_question({"conversation_id": "abc123"}, owner="local")

        assert published == []
