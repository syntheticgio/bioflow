"""Server-sent events, bridged from Redis pub/sub.

SSE rather than WebSocket: the traffic is one-directional, it survives proxies
that mangle upgrades, and browsers reconnect automatically. Worker processes
publish to Redis so their events reach clients connected to the API process.

The stream is partitioned per profile, like every other route that reads user
data -- a job-progress event names a job whose filenames the receiving profile
can then go and fetch. Two channels are subscribed, not one: the caller's own,
and the system channel carrying events that belong to the installation rather
than to anyone's library (storage faults, blob verification, queue-wide
conditions).

Resolving a profile here is organizational, not authentication. The API is
unauthenticated by design, so any client may pass any profile's id and read
that profile's stream, exactly as it may pass any id in the header elsewhere.
See `app/api/deps.py`.
"""

import asyncio
import json

from fastapi import APIRouter, Query, Request
from sse_starlette.sse import EventSourceResponse

from app.api.deps import resolve_owner
from app.db.redis_client import get_redis
from app.logging import get_logger
from app.queue import keys

log = get_logger(__name__)

router = APIRouter(tags=["events"])

# Without traffic, proxies and browsers eventually drop an idle stream.
KEEPALIVE_SECONDS = 20


@router.get("/events")
async def events(request: Request, profile: str | None = Query(None)):
    """Subscribe to one profile's event stream.

    The profile travels as a query parameter because `EventSource` has no way
    to set a header; this is the one route that differs from the rest for that
    reason, and `resolve_owner` is shared with the header dependency so both
    agree on what a valid id is.

    It is resolved *before* the response is returned, not inside the generator.
    Resolving inside would give a stale id a 200 followed by a stream that
    immediately died: the picker recovers from a 400 `profile_unresolved`, and
    it cannot see an exception raised after the headers have gone out.
    """
    owner = await resolve_owner(profile)
    channels = [keys.events_channel(owner), keys.events_channel(keys.SYSTEM_OWNER)]

    async def generator():
        pubsub = get_redis().pubsub()
        await pubsub.subscribe(*channels)
        try:
            while True:
                if await request.is_disconnected():
                    break
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=KEEPALIVE_SECONDS
                )
                if message is None:
                    yield {"event": "ping", "data": "{}"}
                    continue
                try:
                    payload = json.loads(message["data"])
                except (TypeError, ValueError):
                    continue
                yield {
                    "event": payload.get("type", "message"),
                    "data": json.dumps(payload.get("data", {})),
                }
        except asyncio.CancelledError:
            raise
        finally:
            await pubsub.unsubscribe(*channels)
            await pubsub.aclose()

    return EventSourceResponse(generator())
