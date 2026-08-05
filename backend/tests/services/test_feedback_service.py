"""Tests for the feedback webhook notification service.

These are pure unit tests -- they mock the HTTP transport so no network is
needed. The ``TestPost`` and ``TestFormatEmbed`` classes use synchronous tests
(no database, no event loop needed). The ``TestNotifyFeedbackCreated`` class
is async because the service wraps ``_post`` in ``asyncio.to_thread``.
"""

import socket
import urllib.error
from email.message import Message
from unittest.mock import patch

import pytest

from app.services.feedback_service import (
    _format_embed,
    _post,
    notify_feedback_created,
)


class TestFormatEmbed:
    """The Discord webhook payload shape: an embed with fields."""

    def test_builds_an_embed_with_the_comment_as_description(self):
        payload = _format_embed(
            contact="jt@example.com",
            subject="Bug report",
            comment="It broke.",
            feedback_id="64f1a2b3c4d5e6f7",
        )
        assert payload["content"] is None
        embed = payload["embeds"][0]
        assert embed["title"] == "New feedback submission"
        assert embed["description"] == "It broke."

    def test_embed_fields_carry_contact_subject_and_id(self):
        payload = _format_embed(
            contact="jt@example.com",
            subject="Bug report",
            comment="It broke.",
            feedback_id="abc123",
        )
        fields = {f["name"]: f["value"] for f in payload["embeds"][0]["fields"]}
        assert fields["Subject"] == "Bug report"
        assert fields["Contact"] == "jt@example.com"
        assert fields["ID"] == "abc123"

    def test_embed_uses_discord_blurple_color(self):
        payload = _format_embed("a", "b", "c", "d")
        assert payload["embeds"][0]["color"] == 0x5865F2

    def test_embed_url_is_null(self):
        """No public permalink exists in a local deployment."""
        payload = _format_embed("a", "b", "c", "d")
        assert payload["embeds"][0]["url"] is None


class TestPost:
    """The blocking HTTP POST helper: returns bool, never raises."""

    @patch("app.services.feedback_service.urllib.request.urlopen")
    def test_returns_true_on_200(self, mock_urlopen):
        mock_resp = mock_urlopen.return_value.__enter__.return_value
        mock_resp.status = 204
        assert _post("https://example.com/webhook", {"test": True}) is True

    @patch("app.services.feedback_service.urllib.request.urlopen")
    def test_returns_false_on_4xx(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://example.com", code=400, msg="Bad Request",
            hdrs=Message(), fp=None,
        )
        assert _post("https://example.com/webhook", {"test": True}) is False

    @patch("app.services.feedback_service.urllib.request.urlopen")
    def test_returns_false_on_5xx(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://example.com", code=500, msg="Internal Server Error",
            hdrs=Message(), fp=None,
        )
        assert _post("https://example.com/webhook", {"test": True}) is False

    @patch("app.services.feedback_service.urllib.request.urlopen")
    def test_returns_false_on_connection_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("connection refused")
        assert _post("https://example.com/webhook", {"test": True}) is False

    @patch("app.services.feedback_service.urllib.request.urlopen")
    def test_returns_false_on_timeout(self, mock_urlopen):
        mock_urlopen.side_effect = socket.timeout("timed out")
        assert _post("https://example.com/webhook", {"test": True}) is False

    @patch("app.services.feedback_service.urllib.request.urlopen")
    def test_never_raises_on_unexpected_error(self, mock_urlopen):
        mock_urlopen.side_effect = RuntimeError("something broke")
        # Must not raise
        result = _post("https://example.com/webhook", {"test": True})
        assert result is False

    @patch("app.services.feedback_service.urllib.request.urlopen")
    def test_sends_json_with_correct_content_type(self, mock_urlopen):
        mock_resp = mock_urlopen.return_value.__enter__.return_value
        mock_resp.status = 204
        _post("https://example.com/webhook", {"hello": "world"})
        request_obj = mock_urlopen.call_args[0][0]
        # urllib.request.Request stores the URL and method; headers are
        # accessed via header_items() which returns (name, value) pairs.
        # Note: urllib normalizes header names to title case with a lowercase
        # 't' in 'Content-type'.
        headers = {k.lower(): v for k, v in request_obj.header_items()}
        assert headers.get("content-type") == "application/json"
        import json

        body = json.loads(request_obj.data.decode())
        assert body == {"hello": "world"}


class TestNotifyFeedbackCreated:
    """The async wrapper: respects settings, delegates to _post, never raises."""

    @pytest.fixture(autouse=True)
    def _patch_create_task(self, monkeypatch):
        """Replace asyncio.create_task so we can observe the notification fire.

        The production code calls ``asyncio.create_task(notify_feedback_created(...))``
        to avoid delaying the 201 response. In tests, ``create_task`` schedules
        a coroutine on the event loop without running it immediately, which
        means the notification never executes and we can't assert on its side
        effects. We replace it with a synchronous wrapper that captures the
        coroutine so tests can ``await`` it explicitly.
        """
        self._captured: list = []

        def _capture(coro):
            self._captured.append(coro)
            # Return a dummy "task" object (not awaited in production either).
            return None

        monkeypatch.setattr("asyncio.create_task", _capture)

    async def test_skipped_when_not_enabled(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.feedback_service.settings.feedback_enabled", False
        )
        monkeypatch.setattr(
            "app.services.feedback_service.settings.feedback_webhook_url",
            "https://example.com",
        )
        with patch("app.services.feedback_service._post") as mock_post:
            await notify_feedback_created(
                feedback_id="123", contact="jt@example.com",
                subject="Bug", comment="broke",
            )
            mock_post.assert_not_called()
        # No create_task call either, since the service returns early.
        assert len(self._captured) == 0

    async def test_skipped_when_no_url(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.feedback_service.settings.feedback_enabled", True
        )
        monkeypatch.setattr(
            "app.services.feedback_service.settings.feedback_webhook_url", ""
        )
        with patch("app.services.feedback_service._post") as mock_post:
            await notify_feedback_created(
                feedback_id="123", contact="jt@example.com",
                subject="Bug", comment="broke",
            )
            mock_post.assert_not_called()
        assert len(self._captured) == 0

    async def test_calls_post_with_webhook_url_and_payload(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.feedback_service.settings.feedback_enabled", True
        )
        monkeypatch.setattr(
            "app.services.feedback_service.settings.feedback_webhook_url",
            "https://discord.com/api/webhooks/123",
        )
        with patch(
            "app.services.feedback_service._post", return_value=True
        ) as mock_post:
            await notify_feedback_created(
                feedback_id="f1d2", contact="jt@example.com",
                subject="Bug", comment="broke",
            )
            mock_post.assert_called_once()
            call_args = mock_post.call_args
            assert call_args[0][0] == "https://discord.com/api/webhooks/123"
            payload = call_args[0][1]
            assert "embeds" in payload
            assert payload["embeds"][0]["description"] == "broke"

    async def test_failure_is_swallowed(self, monkeypatch):
        """A webhook failure must not raise out of notify_feedback_created."""
        monkeypatch.setattr(
            "app.services.feedback_service.settings.feedback_enabled", True
        )
        monkeypatch.setattr(
            "app.services.feedback_service.settings.feedback_webhook_url",
            "https://discord.com/api/webhooks/123",
        )
        with patch(
            "app.services.feedback_service._post", return_value=False
        ) as mock_post:
            # Must not raise, even though _post returned False
            await notify_feedback_created(
                feedback_id="f1d2", contact="jt@example.com",
                subject="Bug", comment="broke",
            )
            mock_post.assert_called_once()
