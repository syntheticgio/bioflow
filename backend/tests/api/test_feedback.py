"""The feedback HTTP surface: submit and list."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.models.feedback import COMMENT_MAX_LENGTH, Feedback

pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


class TestSubmitFeedback:
    async def test_persists_a_valid_submission(self, client):
        await Feedback.find_all().delete()

        r = await client.post(
            "/api/v1/feedback",
            json={"contact": "jt@example.com", "subject": "Bug", "comment": "It broke."},
        )

        assert r.status_code == 201
        body = r.json()
        assert body["contact"] == "jt@example.com"
        assert body["subject"] == "Bug"
        assert body["comment"] == "It broke."
        assert await Feedback.find_all().count() == 1

    async def test_rejects_a_comment_over_the_limit(self, client):
        r = await client.post(
            "/api/v1/feedback",
            json={
                "contact": "jt@example.com",
                "subject": "Too long",
                "comment": "x" * (COMMENT_MAX_LENGTH + 1),
            },
        )

        assert r.status_code == 422

    async def test_rejects_an_empty_subject(self, client):
        r = await client.post(
            "/api/v1/feedback",
            json={"contact": "jt@example.com", "subject": "", "comment": "hi"},
        )

        assert r.status_code == 422


class TestListFeedback:
    async def test_lists_submissions_newest_first(self, client):
        await Feedback.find_all().delete()
        await Feedback(contact="a", subject="first", comment="one").insert()
        await Feedback(contact="b", subject="second", comment="two").insert()

        r = await client.get("/api/v1/feedback")

        assert r.status_code == 200
        subjects = [item["subject"] for item in r.json()]
        assert subjects == ["second", "first"]
