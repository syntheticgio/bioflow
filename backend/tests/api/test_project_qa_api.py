"""The project Q&A HTTP surface: conversation get/clear, and asking a
question enqueues a job rather than answering synchronously.
"""

import pytest

from app.models import Job
from app.models.conversation import ProjectConversation
from app.services import project_service

pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


@pytest.fixture(autouse=True)
def _no_events(monkeypatch):
    from app.queue import queue

    async def _skip(*args, **kwargs):
        return None

    monkeypatch.setattr(queue, "publish_event", _skip)


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    """The route enqueues a real job (writes to Mongo, the part these tests
    assert on) but `queue.enqueue` also pushes to Redis, which is not
    connected in this ASGI-transport test setup (no lifespan trigger). Patch
    only the Redis push, not enqueue itself, so the Mongo-side Job document
    this test suite actually checks is still real."""
    from app.queue import queue

    async def _skip_redis_push(job, *, delay_seconds=0):
        return None

    monkeypatch.setattr(queue, "_push_to_redis", _skip_redis_push)


async def _project(owner: str):
    return await project_service.create_project(name="p", owner=owner, parent_id=None)


class TestGetConversation:
    async def test_creates_an_empty_one_on_first_access(self, client, two_profiles):
        project = await _project(two_profiles["a"].owner_id())

        response = await client.get(
            f"/api/v1/projects/{project.id}/qa/conversation", headers=two_profiles["a_headers"]
        )

        assert response.status_code == 200
        assert response.json()["turns"] == []

    async def test_is_owner_scoped(self, client, two_profiles):
        """A conversation with turns under owner A must not appear for owner
        B on the same project id -- B gets their own empty one instead."""
        from app.models.base import utcnow
        from app.models.conversation import ConversationTurn

        project = await _project(two_profiles["a"].owner_id())
        convo = ProjectConversation(
            owner=two_profiles["a"].owner_id(),
            project_id=project.id,
            turns=[ConversationTurn(role="user", content="secret", created_at=utcnow())],
        )
        await convo.insert()

        response = await client.get(
            f"/api/v1/projects/{project.id}/qa/conversation", headers=two_profiles["b_headers"]
        )

        assert response.json()["turns"] == []

    async def test_missing_profile_header_is_400(self, client, two_profiles):
        project = await _project(two_profiles["a"].owner_id())

        response = await client.get(f"/api/v1/projects/{project.id}/qa/conversation")

        assert response.status_code == 400


class TestAsk:
    async def test_enqueues_a_job_and_returns_its_id(self, client, two_profiles):
        project = await _project(two_profiles["a"].owner_id())

        response = await client.post(
            f"/api/v1/projects/{project.id}/qa/ask",
            json={"question": "how many files?"},
            headers=two_profiles["a_headers"],
        )

        assert response.status_code == 200
        job_id = response.json()["job_id"]
        job = await Job.get(job_id)
        assert job.type == "answer_project_question"
        assert job.owner == two_profiles["a"].owner_id()

    async def test_two_identical_questions_in_a_row_both_enqueue(self, client, two_profiles):
        """No dedup_key -- a deliberate "actually, ask that again" must not
        be silently dropped."""
        project = await _project(two_profiles["a"].owner_id())
        body = {"question": "same question"}

        r1 = await client.post(
            f"/api/v1/projects/{project.id}/qa/ask", json=body, headers=two_profiles["a_headers"]
        )
        r2 = await client.post(
            f"/api/v1/projects/{project.id}/qa/ask", json=body, headers=two_profiles["a_headers"]
        )

        assert r1.json()["job_id"] != r2.json()["job_id"]

    async def test_ask_creates_the_conversation_if_none_exists_yet(self, client, two_profiles):
        project = await _project(two_profiles["a"].owner_id())

        await client.post(
            f"/api/v1/projects/{project.id}/qa/ask",
            json={"question": "q"},
            headers=two_profiles["a_headers"],
        )

        convo = await ProjectConversation.find_one(
            ProjectConversation.owner == two_profiles["a"].owner_id(),
            ProjectConversation.project_id == project.id,
        )
        assert convo is not None

    async def test_the_job_payload_names_the_new_conversation(self, client, two_profiles):
        project = await _project(two_profiles["a"].owner_id())

        response = await client.post(
            f"/api/v1/projects/{project.id}/qa/ask",
            json={"question": "q"},
            headers=two_profiles["a_headers"],
        )

        job = await Job.get(response.json()["job_id"])
        convo = await ProjectConversation.find_one(
            ProjectConversation.owner == two_profiles["a"].owner_id(),
            ProjectConversation.project_id == project.id,
        )
        assert job.payload["conversation_id"] == str(convo.id)
        assert job.payload["question"] == "q"
        assert job.payload["project_id"] == str(project.id)


class TestClearConversation:
    async def test_clears_turns_and_compaction_state(self, client, two_profiles):
        from app.models.base import utcnow
        from app.models.conversation import ConversationTurn

        project = await _project(two_profiles["a"].owner_id())
        convo = ProjectConversation(
            owner=two_profiles["a"].owner_id(),
            project_id=project.id,
            turns=[ConversationTurn(role="user", content="x", created_at=utcnow())],
            compacted_summary="old",
            compacted_through=1,
        )
        await convo.insert()

        response = await client.delete(
            f"/api/v1/projects/{project.id}/qa/conversation", headers=two_profiles["a_headers"]
        )

        assert response.status_code == 204
        refreshed = await ProjectConversation.get(convo.id)
        assert refreshed.turns == []
        assert refreshed.compacted_summary is None
        assert refreshed.compacted_through == 0

    async def test_clearing_with_no_existing_conversation_is_not_an_error(self, client, two_profiles):
        project = await _project(two_profiles["a"].owner_id())

        response = await client.delete(
            f"/api/v1/projects/{project.id}/qa/conversation", headers=two_profiles["a_headers"]
        )

        assert response.status_code == 204

    async def test_cannot_clear_another_owners_conversation(self, client, two_profiles):
        from app.models.base import utcnow
        from app.models.conversation import ConversationTurn

        project = await _project(two_profiles["a"].owner_id())
        convo = ProjectConversation(
            owner=two_profiles["a"].owner_id(),
            project_id=project.id,
            turns=[ConversationTurn(role="user", content="x", created_at=utcnow())],
        )
        await convo.insert()

        await client.delete(
            f"/api/v1/projects/{project.id}/qa/conversation", headers=two_profiles["b_headers"]
        )

        refreshed = await ProjectConversation.get(convo.id)
        assert len(refreshed.turns) == 1
