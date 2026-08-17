"""Tests for reading and reaping the worker heartbeat hash.

The hash retains an entry for any worker that did not shut down gracefully,
which is what put a phantom "unknown" node in the settings table (#451).
"""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import pytest
from app.queue import worker_registry


@pytest.fixture
async def fake_redis():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


def _blob(age: timedelta = timedelta(0), **overrides) -> str:
    payload = {
        "last_seen": (datetime.now(UTC) - age).isoformat(),
        "slots": 4,
        "running": [],
        "draining": False,
        "node_id": "primary",
    }
    payload.update(overrides)
    return json.dumps(payload)


async def _seed(redis, **workers):
    for worker_id, blob in workers.items():
        await redis.hset("bp:workers", worker_id, blob)


async def _live(fake_redis) -> dict[str, dict]:
    with patch("app.queue.worker_registry.get_redis", return_value=fake_redis):
        return dict(await worker_registry.live_workers())


class TestLiveWorkers:
    async def test_empty_hash(self, fake_redis):
        assert await _live(fake_redis) == {}

    async def test_fresh_worker_survives(self, fake_redis):
        await _seed(fake_redis, **{"w:1": _blob()})
        live = await _live(fake_redis)
        assert list(live) == ["w:1"]
        assert live["w:1"]["node_id"] == "primary"

    async def test_offline_but_recent_worker_survives(self, fake_redis):
        """Well past the 60s "offline" line, nowhere near the reap cutoff."""
        await _seed(fake_redis, **{"w:1": _blob(age=timedelta(hours=6))})
        assert list(await _live(fake_redis)) == ["w:1"]
        assert await fake_redis.hkeys("bp:workers") == ["w:1"]

    async def test_stale_worker_is_dropped_and_deleted(self, fake_redis):
        await _seed(fake_redis, **{"w:1": _blob(age=timedelta(days=11))})
        assert await _live(fake_redis) == {}
        assert await fake_redis.hkeys("bp:workers") == []

    async def test_reap_keeps_the_live_ones(self, fake_redis):
        await _seed(
            fake_redis,
            **{
                "live:1": _blob(),
                "dead:1": _blob(age=timedelta(days=11)),
                "dead:2": _blob(age=timedelta(days=400)),
            },
        )
        assert list(await _live(fake_redis)) == ["live:1"]
        assert await fake_redis.hkeys("bp:workers") == ["live:1"]

    async def test_unparseable_blob_is_reaped(self, fake_redis):
        await _seed(fake_redis, **{"junk:1": "not json at all"})
        assert await _live(fake_redis) == {}
        assert await fake_redis.hkeys("bp:workers") == []

    async def test_non_object_blob_is_reaped(self, fake_redis):
        """Valid JSON that isn't a dict would crash every reader downstream."""
        await _seed(fake_redis, **{"junk:1": json.dumps([1, 2, 3])})
        assert await _live(fake_redis) == {}
        assert await fake_redis.hkeys("bp:workers") == []

    async def test_missing_last_seen_is_reaped(self, fake_redis):
        """No timestamp means no way to tell if it is alive; treat as dead."""
        blob = json.loads(_blob())
        del blob["last_seen"]
        await _seed(fake_redis, **{"w:1": json.dumps(blob)})
        assert await _live(fake_redis) == {}
        assert await fake_redis.hkeys("bp:workers") == []

    async def test_naive_timestamp_is_read_as_utc(self, fake_redis):
        """A tz-naive last_seen must not make the comparison raise."""
        naive = datetime.now(UTC).replace(tzinfo=None).isoformat()
        await _seed(fake_redis, **{"w:1": _blob(last_seen=naive)})
        assert list(await _live(fake_redis)) == ["w:1"]

    async def test_worker_without_node_id_is_kept_if_fresh(self, fake_redis):
        """The registry reaps by age only; node_id is the caller's business.

        `/nodes` skips these, but the footer's worker count should still see a
        worker that is genuinely heartbeating right now.
        """
        blob = json.loads(_blob())
        del blob["node_id"]
        await _seed(fake_redis, **{"w:1": json.dumps(blob)})
        assert list(await _live(fake_redis)) == ["w:1"]

    async def test_read_failure_degrades_to_empty(self):
        """Redis being down yields no workers rather than a 500 on the page
        someone opened because something already looked wrong."""
        broken = AsyncMock()
        broken.hgetall.side_effect = ConnectionError("redis is down")
        with patch("app.queue.worker_registry.get_redis", return_value=broken):
            assert dict(await worker_registry.live_workers()) == {}

    async def test_reap_failure_still_returns_the_live_workers(self, fake_redis):
        """Cleanup is best-effort; the next read tries again."""
        await _seed(
            fake_redis,
            **{"live:1": _blob(), "dead:1": _blob(age=timedelta(days=11))},
        )
        with (
            patch("app.queue.worker_registry.get_redis", return_value=fake_redis),
            patch.object(fake_redis, "hdel", side_effect=ConnectionError("redis is down")),
        ):
            live = dict(await worker_registry.live_workers())
        assert list(live) == ["live:1"]
        # Not reaped, but not reported either.
        assert set(await fake_redis.hkeys("bp:workers")) == {"live:1", "dead:1"}
