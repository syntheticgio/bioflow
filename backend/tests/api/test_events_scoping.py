"""The SSE stream carries one profile's events, and the installation's.

This is the last router to be scoped, and the one where a leak is most direct:
a `job.progress` event names a job the receiving browser will then fetch, with
the filename in the response.

Every test asserts both directions. "B's event never appeared" is true of a
stream that delivers nothing at all -- including one still subscribed to the
old global channel under a different name -- so each test also requires the
event that *should* arrive to arrive, in the same window.
"""

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from app.api.v1 import events as events_route
from app.queue import keys, queue

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]

# Long enough for the generator to subscribe and for the republish loop below to
# get several attempts in; short enough that a genuine failure is not a stall.
LISTEN_SECONDS = 1.5
REPUBLISH_INTERVAL = 0.05


@pytest.fixture
async def fake_redis(monkeypatch):
    """One fakeredis shared by the route and the test's publisher.

    Patched into both modules by name: `events.py` and `queue.py` each bound
    `get_redis` at import, so patching `app.db.redis_client` alone would leave
    both of them holding the real one.
    """
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(events_route, "get_redis", lambda: client)
    monkeypatch.setattr(queue, "get_redis", lambda: client)
    yield client
    await client.aclose()


async def _republish(events: list[tuple[str, str]]) -> None:
    """Publish each (type, owner) on a loop until cancelled.

    Republishing rather than publishing once, because pub/sub has no backlog:
    the route subscribes only when its response body starts streaming, and a
    message published before that moment is not delayed, it is gone. Waiting
    for a first line instead would take the full 20-second keepalive, since a
    quiet stream sends nothing until then.
    """
    while True:
        for event_type, owner in events:
            await queue.publish_event(event_type, {"job_id": "j1"}, owner=owner)
        await asyncio.sleep(REPUBLISH_INTERVAL)


async def _collect_event_names(profile: str, published: list[tuple[str, str]]):
    """Names of the events this profile's stream yields in the window.

    The route is called directly and its body iterator drained, rather than
    driving it over `client.stream`. Not a shortcut: the generator runs until
    `request.is_disconnected()` says otherwise, and under httpx's ASGITransport
    closing the stream never makes that true, so the request hangs until the
    test suite is killed. Going through the returned response also skips SSE
    wire encoding, which is `sse_starlette`'s to get right and not this
    module's -- what is under test is which events reach the iterator at all.
    """
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    response = await events_route.events(request, profile=profile)
    stream = response.body_iterator

    seen: set[str] = set()
    publisher = asyncio.create_task(_republish(published))

    async def read():
        async for chunk in stream:
            name = chunk.get("event")
            if name and name != "ping":
                seen.add(name)

    try:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(read(), timeout=LISTEN_SECONDS)
    finally:
        publisher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await publisher
        await stream.aclose()
    return seen


class TestStreamIsScopedToItsProfile:
    async def test_a_receives_its_own_and_not_bs(self, two_profiles, fake_redis):
        a_owner = two_profiles["a"].owner_id()
        b_owner = two_profiles["b"].owner_id()

        seen = await _collect_event_names(
            a_owner,
            [("job.succeeded", a_owner), ("job.failed", b_owner)],
        )

        assert "job.succeeded" in seen
        assert "job.failed" not in seen

    async def test_b_receives_its_own_and_not_as(self, two_profiles, fake_redis):
        """The mirror image, and not redundant: a stream wired to one hardcoded
        channel passes the test above whenever that channel happens to be A's."""
        a_owner = two_profiles["a"].owner_id()
        b_owner = two_profiles["b"].owner_id()

        seen = await _collect_event_names(
            b_owner,
            [("job.succeeded", a_owner), ("job.failed", b_owner)],
        )

        assert "job.failed" in seen
        assert "job.succeeded" not in seen


class TestSystemEventsReachEveryone:
    """Both profiles subscribe to the system channel as well as their own.

    This is the assertion that fails if someone later simplifies the route's
    two-channel subscription down to one: storage faults and missing blobs
    would then be delivered to nobody, silently, since the publisher would go
    on succeeding.
    """

    async def test_a_sees_a_system_event(self, two_profiles, fake_redis):
        a_owner = two_profiles["a"].owner_id()

        seen = await _collect_event_names(
            a_owner, [("storage.unavailable", keys.SYSTEM_OWNER)]
        )

        assert "storage.unavailable" in seen

    async def test_b_sees_the_same_system_event(self, two_profiles, fake_redis):
        b_owner = two_profiles["b"].owner_id()

        seen = await _collect_event_names(
            b_owner, [("storage.unavailable", keys.SYSTEM_OWNER)]
        )

        assert "storage.unavailable" in seen


class TestUnresolvedProfileIsRefusedBeforeTheStream:
    """A 400 the picker can act on, not a 200 followed by a dead stream.

    `EventSource` reconnects on error by itself, so a stream that opens and
    then fails is a reconnect loop rather than a single visible failure -- which
    is why the profile is resolved before the response is returned.
    """

    async def test_a_missing_profile(self, client):
        response = await client.get("/api/v1/events")

        assert response.status_code == 400
        assert response.json()["code"] == "profile_unresolved"

    async def test_a_malformed_profile(self, client):
        response = await client.get("/api/v1/events?profile=not-an-object-id")

        assert response.status_code == 400
        assert response.json()["code"] == "profile_unresolved"

    async def test_a_well_formed_id_naming_no_profile(self, client):
        response = await client.get("/api/v1/events?profile=68a0000000000000000000ff")

        assert response.status_code == 400
        assert response.json()["code"] == "profile_unresolved"
