"""Lease extension: a handler declaring it needs longer than the default.

The heartbeat renews every in-flight job on a fixed interval, which covers a
merely *slow* job. It does not cover a paused VM or a stalled loop -- the
laptop-lid case reap_expired.lua exists for. A handler that asked for an hour
and silently got the 30s default is one lid-close away from being reaped and
double-run, which is why these callers are not decorative.
"""

from datetime import UTC, datetime

import pytest

from app.queue.registry import JobContext


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


async def _noop_mongo(job_ids, epochs, ttls, now):
    """Stand-in for the Mongo half of heartbeat, which needs a database."""
    return None


class TestExtendLease:
    def test_defaults_to_no_override(self):
        ctx = JobContext(job_id="j1", payload={}, epoch=0, attempts=0)
        assert ctx.lease_override_seconds is None

    def test_records_the_requested_seconds(self):
        ctx = JobContext(job_id="j1", payload={}, epoch=0, attempts=0)
        ctx.extend_lease(3600)
        assert ctx.lease_override_seconds == 3600

    def test_keeps_the_longest_request(self):
        """A handler with several long phases must not shorten its own lease by
        asking for less on a later phase than it did on an earlier one."""
        ctx = JobContext(job_id="j1", payload={}, epoch=0, attempts=0)
        ctx.extend_lease(3600)
        ctx.extend_lease(60)
        assert ctx.lease_override_seconds == 3600

    def test_ignores_a_nonpositive_request(self):
        ctx = JobContext(job_id="j1", payload={}, epoch=0, attempts=0)
        ctx.extend_lease(0)
        ctx.extend_lease(-5)
        assert ctx.lease_override_seconds is None

    def test_still_invokes_the_callback_when_one_is_set(self):
        """The callback stays supported so the worker can react immediately
        rather than waiting for the next heartbeat tick."""
        seen = []
        ctx = JobContext(job_id="j1", payload={}, epoch=0, attempts=0)
        ctx._extend_cb = seen.append
        ctx.extend_lease(120)
        assert seen == [120]


class TestHeartbeatTtls:
    """queue.heartbeat's Redis half, exercised directly.

    The Mongo half needs a database and is covered by the container suite; what
    matters here is that the RUNNING zset score -- the value reap_expired.lua
    compares against -- reflects the per-job TTL rather than the global default.
    """

    async def test_uses_the_default_ttl_when_no_override(self, redis, monkeypatch):
        from app.config import settings
        from app.queue import queue

        monkeypatch.setattr(queue, "get_redis", lambda: redis)
        monkeypatch.setattr(queue, "_heartbeat_mongo", _noop_mongo)

        await queue.heartbeat(["job1"], {"job1": 0})

        score = await redis.zscore("bp:q:running", "job1")
        now_ms = _now_ms()
        expected = now_ms + settings.lease_ttl_seconds * 1000
        assert abs(score - expected) < 5000

    async def test_a_longer_override_pushes_the_expiry_out(self, redis, monkeypatch):
        from app.queue import queue

        monkeypatch.setattr(queue, "get_redis", lambda: redis)
        monkeypatch.setattr(queue, "_heartbeat_mongo", _noop_mongo)

        await queue.heartbeat(["job1"], {"job1": 0}, ttls={"job1": 3600})

        score = await redis.zscore("bp:q:running", "job1")
        expected = _now_ms() + 3600 * 1000
        assert abs(score - expected) < 5000

    async def test_each_job_gets_its_own_ttl(self, redis, monkeypatch):
        """A quick job alongside a long one must not inherit the long lease."""
        from app.config import settings
        from app.queue import queue

        monkeypatch.setattr(queue, "get_redis", lambda: redis)
        monkeypatch.setattr(queue, "_heartbeat_mongo", _noop_mongo)

        await queue.heartbeat(
            ["slow", "quick"], {"slow": 0, "quick": 0}, ttls={"slow": 3600}
        )

        slow = await redis.zscore("bp:q:running", "slow")
        quick = await redis.zscore("bp:q:running", "quick")
        assert slow - quick > 3000 * 1000
        assert abs(quick - (_now_ms() + settings.lease_ttl_seconds * 1000)) < 5000
