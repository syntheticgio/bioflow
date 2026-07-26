"""Server-sent events, bridged from Redis pub/sub.

SSE rather than WebSocket: the traffic is one-directional, it survives proxies
that mangle upgrades, and browsers reconnect automatically. Worker processes
publish to Redis so their events reach clients connected to the API process.
"""

import asyncio
import json

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from app.db.redis_client import get_redis
from app.logging import get_logger
from app.queue import keys

log = get_logger(__name__)

router = APIRouter(tags=["events"])

# Without traffic, proxies and browsers eventually drop an idle stream.
KEEPALIVE_SECONDS = 20


@router.get("/events")
async def events(request: Request):
    async def generator():
        pubsub = get_redis().pubsub()
        await pubsub.subscribe(keys.EVENTS)
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
            await pubsub.unsubscribe(keys.EVENTS)
            await pubsub.aclose()

    return EventSourceResponse(generator())
