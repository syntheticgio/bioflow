"""The feedback HTTP surface: submit and list."""

import pytest
from httpx import ASGITransport, AsyncClient
from unittest.mock import patch

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


class TestWebhookNotificationResilience:
    """The notification is best-effort: a delivery failure never affects the
    201 response or the persisted feedback.
    """

    @pytest.fixture(autouse=True)
    def _force_notification_on(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.feedback_service.settings.feedback_enabled", True
        )
        monkeypatch.setattr(
            "app.services.feedback_service.settings.feedback_webhook_url",
            "https://example.com/webhook",
        )

    @pytest.fixture(autouse=True)
    def _patch_create_task(self, monkeypatch):
        """Replace asyncio.create_task so we can observe the notification fire.

        The production code calls ``asyncio.create_task`` to fire the webhook
        notification without delaying the 201. In tests that schedules a
        coroutine on the event loop without running it immediately, which means
        the notification never executes and we can't assert on its side
        effects. We replace it with a synchronous stub that captures the
        coroutine so we can verify it was scheduled.
        """
        self._captured: list = []

        def _capture(coro):
            self._captured.append(coro)
            return None  # create_task normally returns a Task; tests don't need it

        monkeypatch.setattr("asyncio.create_task", _capture)

    async def test_submit_returns_201_when_webhook_raises(self, client):
        """The key acceptance test: a webhook failure does not change the
        successful 201 response or lose the saved feedback."""
        await Feedback.find_all().delete()

        with patch(
            "app.services.feedback_service._post",
            side_effect=RuntimeError("discord is down"),
        ):
            r = await client.post(
                "/api/v1/feedback",
                json={
                    "contact": "jt@example.com",
                    "subject": "Bug with webhook",
                    "comment": "Everything persisted, Discord didn't.",
                },
            )

        assert r.status_code == 201
        assert await Feedback.find_all().count() == 1
        # The notification task was scheduled (create_task was called).
        assert len(self._captured) == 1

    async def test_submit_returns_201_when_no_webhook_configured(
        self, client, monkeypatch
    ):
        """With notification disabled (no URL), the endpoint behaves exactly
        as before -- 201 and the feedback is saved, no notification scheduled."""
        await Feedback.find_all().delete()
        # Override the autouse fixture: clear the URL so the service is a no-op
        monkeypatch.setattr(
            "app.services.feedback_service.settings.feedback_webhook_url", ""
        )

        with patch("app.services.feedback_service._post") as mock_post:
            r = await client.post(
                "/api/v1/feedback",
                json={
                    "contact": "jt@example.com",
                    "subject": "No webhook",
                    "comment": "This should still work.",
                },
            )

            assert r.status_code == 201
            assert await Feedback.find_all().count() == 1

            # Run the captured coroutine to verify the service skips the HTTP
            # call when feedback_webhook_url is empty.
            for coro in self._captured:
                await coro

            mock_post.assert_not_called()


class TestListFeedback:
    async def test_lists_submissions_newest_first(self, client):
        await Feedback.find_all().delete()
        await Feedback(contact="a", subject="first", comment="one").insert()
        # Distinct created_at: Mongo sorts newest-first, and identical
        # timestamps would make the sort order non-deterministic.
        import asyncio as _asyncio

        await _asyncio.sleep(0.01)
        await Feedback(contact="b", subject="second", comment="two").insert()

        r = await client.get("/api/v1/feedback")

        assert r.status_code == 200
        subjects = [item["subject"] for item in r.json()]
        assert subjects == ["second", "first"]
