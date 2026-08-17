"""Events land on their owner's channel and on no one else's.

Both directions are asserted throughout, and that is the point of the module.
"B's channel received nothing" passes just as well against a publisher wired to
a single hardcoded channel -- A's events are not on B's channel there either.
CLAUDE.md records ten isolation tests that shipped green for exactly that
reason, so every test here also asserts that A's channel *did* receive it.

Owners are non-"local" on purpose, matching `test_queue_owner.py`: "local" is
the default every document inherits from `TimestampedDocument`, so an assertion
against it holds whether or not anything was threaded through.
"""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import fakeredis.aioredis
import pytest
from app.models import Job, JobLease, JobState, JobTiming
from app.queue import keys, queue

pytestmark = [pytest.mark.asyncio(loop_scope="module")]

OWNER_A = "68a0000000000000000000aa"
OWNER_B = "68a0000000000000000000bb"


class Collector:
    """Subscribes to a channel and records what arrives.

    Reads through `get_message` rather than a background listener task so the
    test drives the polling itself: an event published before the subscription
    is established is simply lost, and a listener started concurrently makes
    that a race rather than a fixed ordering.
    """

    def __init__(self, redis):
        self._redis = redis
        self._pubsub = redis.pubsub()

    async def subscribe(self, *channels: str) -> None:
        self._channels = channels
        await self._pubsub.subscribe(*channels)
        # Consume the subscribe confirmations. `ignore_subscribe_messages=True`
        # does not skip past one to the next message -- it returns None for it,
        # one call per confirmation. Draining without this priming stops on that
        # None and reports an empty channel, which reads exactly like a leak
        # test passing: every assertion of "nothing arrived" holds, and every
        # assertion of "it did arrive" fails for a reason that has nothing to do
        # with the code under test.
        for _ in channels:
            await self._pubsub.get_message(
                ignore_subscribe_messages=True, timeout=0.05
            )

    async def drain(self) -> list[dict]:
        """Every message waiting right now, decoded.

        The sleep is not a guess at timing. fakeredis hands a published message
        to its subscribers from a background task, so a `publish` awaited on
        this same loop is not yet readable when it returns -- the delivery task
        has not been given a chance to run. Yielding once is what lets it.
        """
        await asyncio.sleep(0.05)
        out = []
        while True:
            message = await self._pubsub.get_message(
                ignore_subscribe_messages=True, timeout=0.05
            )
            if message is None:
                return out
            out.append(json.loads(message["data"]))

    async def types(self) -> list[str]:
        return [m["type"] for m in await self.drain()]

    async def aclose(self) -> None:
        await self._pubsub.unsubscribe(*self._channels)
        await self._pubsub.aclose()


@pytest.fixture
async def redis(monkeypatch):
    """A fakeredis that `publish_event` and the handlers will actually use.

    Patched onto the `queue` module rather than `app.db.redis_client`, because
    that is where the name `publish_event` resolves: `queue.py` imports
    `get_redis` at module load, so patching the source module would leave this
    binding pointing at the real client.
    """
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(queue, "get_redis", lambda: client)
    yield client
    await client.aclose()


@pytest.fixture
async def channels(redis):
    """One collector per channel: A's, B's, and the system channel."""
    made = {}
    for label, owner in (("a", OWNER_A), ("b", OWNER_B), ("system", keys.SYSTEM_OWNER)):
        c = Collector(redis)
        await c.subscribe(keys.events_channel(owner))
        made[label] = c
    yield made
    for c in made.values():
        await c.aclose()


class TestPublishEventRouting:
    async def test_an_owners_event_reaches_that_owner_and_no_other(self, channels):
        await queue.publish_event("job.succeeded", {"job_id": "j1"}, owner=OWNER_A)

        assert await channels["a"].types() == ["job.succeeded"]
        assert await channels["b"].types() == []

    async def test_the_reverse_direction_too(self, channels):
        """Deliberately redundant-looking, and not redundant.

        A publisher that resolved every owner to one hardcoded channel would
        pass the test above if that channel happened to be A's. Publishing as B
        and finding it on B's channel is what rules that out.
        """
        await queue.publish_event("job.failed", {"job_id": "j2"}, owner=OWNER_B)

        assert await channels["b"].types() == ["job.failed"]
        assert await channels["a"].types() == []

    async def test_an_owners_event_does_not_reach_the_system_channel(self, channels):
        """The system channel is subscribed by *every* client, so anything
        leaking onto it leaks to everyone -- the failure this design exists to
        prevent, arriving by the back door."""
        await queue.publish_event("job.progress", {"job_id": "j3"}, owner=OWNER_A)

        assert await channels["system"].types() == []

    async def test_a_system_event_reaches_the_system_channel_only(self, channels):
        await queue.publish_event(
            "storage.unavailable", {"detail": "gone"}, owner=keys.SYSTEM_OWNER
        )

        assert await channels["system"].types() == ["storage.unavailable"]
        assert await channels["a"].types() == []
        assert await channels["b"].types() == []

    async def test_the_payload_survives_intact(self, channels):
        await queue.publish_event("job.progress", {"job_id": "j4", "pct": 42}, owner=OWNER_A)

        assert await channels["a"].drain() == [
            {"type": "job.progress", "data": {"job_id": "j4", "pct": 42}}
        ]


class TestEnqueuePublishesToItsOwner:
    """`enqueue` is the one publisher whose owner comes from its own argument
    rather than from a job document, so it is worth covering separately."""

    pytestmark = pytest.mark.usefixtures("beanie_models")

    @pytest.fixture(autouse=True)
    def _no_push(self, monkeypatch):
        """Stub the Redis *queue* push, keep the event publish.

        The inverse of `test_queue_owner.py`'s fixture, for the inverse reason:
        there the publish was noise around a Mongo insert, here it is the
        subject.
        """

        async def _skip(*args, **kwargs):
            return None

        monkeypatch.setattr(queue, "_push_to_redis", _skip)

    async def test_job_enqueued_goes_to_the_enqueueing_owner(self, channels):
        job = await queue.enqueue(
            "noop", owner=OWNER_A, dedup_key=f"events:{uuid.uuid4().hex}"
        )

        assert job is not None
        assert await channels["a"].types() == ["job.enqueued"]
        assert await channels["b"].types() == []


class TestCompleteRoutesByJobOwner:
    pytestmark = pytest.mark.usefixtures("beanie_models")

    @pytest.fixture(autouse=True)
    def _no_redis_side_effects(self, monkeypatch):
        """`complete` also releases the lease and unblocks dependents, neither
        of which this module is about."""

        async def _skip(*args, **kwargs):
            return None

        monkeypatch.setattr(queue, "release", _skip)
        monkeypatch.setattr(queue, "_release_dependents", _skip)

    async def _running_job(self, owner: str) -> Job:
        job = Job(
            type="noop",
            owner=owner,
            state=JobState.RUNNING,
            lease=JobLease(
                worker_id="w1",
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
                heartbeat_at=datetime.now(UTC),
                epoch=1,
            ),
            timing=JobTiming(enqueued_at=datetime.now(UTC)),
        )
        await job.insert()
        return job

    async def test_the_terminal_event_goes_to_the_jobs_owner(self, channels):
        job = await self._running_job(OWNER_B)

        assert await queue.complete(str(job.id), 1, state=JobState.SUCCEEDED) is True

        assert await channels["b"].types() == ["job.succeeded"]
        assert await channels["a"].types() == []

    async def test_a_vanished_job_document_falls_back_to_system_not_local(
        self, channels, redis, monkeypatch
    ):
        """The fallback that would have been wrong, pinned.

        `complete` re-reads the job for its start time, and that read can come
        back None -- the guarded update works off the id alone. The tempting
        fallback is `"local"`, and `"local"` is a real profile's owner:
        whichever one adopted the pre-profiles library. Falling back to it
        would drop a stranger's job event into that person's stream. Simulated
        by making the re-read miss while the update still matches, which is the
        race as it would actually happen.
        """
        local = Collector(redis)
        await local.subscribe(keys.events_channel("local"))
        job = await self._running_job("local")

        async def _gone(*args, **kwargs):
            return None

        monkeypatch.setattr(queue.Job, "get", _gone)

        assert await queue.complete(str(job.id), 1, state=JobState.FAILED) is True

        assert await channels["system"].types() == ["job.failed"]
        assert await local.types() == []
        await local.aclose()
