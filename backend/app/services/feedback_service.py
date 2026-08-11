"""Best-effort Discord webhook notifications for feedback submissions.

When a user submits feedback from the Help > Feedback page, the record is
persisted to Mongo first. This module then fires a webhook notification
(a Discord-compatible embed) without ever affecting the 201 response: every
error path is logged and swallowed, so a downed webhook, a timeout, or a
transient network glitch degrades silently rather than losing the feedback
or surfacing an error to the user.

Uses stdlib ``urllib`` rather than ``httpx`` (which is dev-only and not
installed in the runtime Docker image), matching the pattern in
``structure_lookup.py`` and ``ai/adapters.py``. The blocking socket runs in
a thread via ``asyncio.to_thread``.
"""

import asyncio
import json
import urllib.error
import urllib.request

from app.config import settings
from app.logging import get_logger

log = get_logger(__name__)

# Generous for a local network call, but short enough that a hung webhook
# does not hold the request open indefinitely. The notification is
# fire-and-forget (asyncio.create_task), so even this timeout only costs a
# background task, never the user's submission.
_TIMEOUT_SECONDS = 10.0


def _post(url: str, payload: dict) -> bool:
    """POST a JSON payload to a webhook URL. Returns True on HTTP 2xx.

    Isolated from ``notify_feedback_created`` so tests can replace it without
    touching the network. Every error -- timeout, connection refused, DNS
    failure, non-2xx status -- returns False rather than raising, so the
    caller's contract (never raise) holds.
    """
    data = json.dumps(payload).encode()
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            return 200 <= response.status < 300
    except urllib.error.HTTPError as exc:
        log.warning("feedback_webhook_http_error", status=exc.code, error=str(exc))
        return False
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        log.warning("feedback_webhook_transport_error", error=str(exc))
        return False
    except Exception as exc:  # noqa: BLE001 -- notification must never crash the request
        log.warning("feedback_webhook_unexpected_error", error=str(exc))
        return False


def _format_embed(contact: str, subject: str, comment: str, feedback_id: str) -> dict:
    """Build a Discord webhook payload as an embed.

    Discord's embed format gives a structured, readable message in the channel:
    a titled card with the comment as description and fields for the metadata.
    The ``url`` field is intentionally null -- this is a local deployment with
    no public permalink to a feedback record, so there is nothing meaningful
    to link.
    """
    return {
        "content": None,
        "embeds": [
            {
                "title": "New feedback submission",
                "description": comment,
                "url": None,
                "fields": [
                    {"name": "Subject", "value": subject, "inline": True},
                    {"name": "Contact", "value": contact, "inline": True},
                    {"name": "ID", "value": feedback_id, "inline": True},
                ],
                "color": 0x5865F2,
            }
        ],
    }


async def notify_feedback_created(
    *, feedback_id: str, contact: str, subject: str, comment: str
) -> None:
    """Push a new feedback submission to the configured Discord webhook.

    Called *after* the ``Feedback`` document has been inserted, so the record
    is never at risk regardless of whether this succeeds. The function is
    designed to be called via ``asyncio.create_task``: it catches every error
    internally and never raises, so a failure only loses the notification,
    not the data or the user's 201 response.

    When ``settings.feedback_enabled`` is false or no webhook URL is
    configured, this is a no-op -- useful for local dev without a Discord
    server.
    """
    if not settings.feedback_enabled or not settings.feedback_webhook_url:
        log.debug("feedback_notification_skipped", reason="disabled_or_no_url")
        return

    payload = _format_embed(contact, subject, comment, feedback_id)
    success = await asyncio.to_thread(
        _post, settings.feedback_webhook_url, payload
    )
    if success:
        log.info("feedback_webhook_sent", feedback_id=feedback_id)
    else:
        log.warning("feedback_webhook_failed", feedback_id=feedback_id)


__all__ = ["notify_feedback_created", "_post", "_format_embed"]
