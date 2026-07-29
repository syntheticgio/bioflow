"""bp:cancel must not accumulate ids for jobs that are already finished.

Every worker runs SMEMBERS bp:cancel once a second in _cancel_watch_loop, so a
stale entry is a cost paid forever by every worker. release.lua already clears
the flag on its drop path; these are the routes that bypass it.
"""

import pytest

from tests.queue.test_lifecycle import LEASE_MS, NOW_MS, claim


class TestReaperClearsCancel:
    async def test_requeued_job_keeps_its_cancel_entry(self, redis, scripts, job_factory):
        """The flag must survive a requeue: the job runs again and still needs
        to observe that it was cancelled. This is the case the fix must NOT
        break, so it is asserted against the raw script."""
        await job_factory("job1")
        await claim(scripts)
        await redis.sadd("bp:cancel", "job1")

        await scripts["reap_expired"](
            keys=["bp:q:running", "bp:q:ready"],
            args=[NOW_MS + LEASE_MS + 1, 100],
        )

        assert await redis.sismember("bp:cancel", "job1") == 1
        assert await redis.zscore("bp:q:ready", "job1") is not None


class TestFailBlockedJobClearsCancel:
    async def test_clears_the_flag_for_a_dependency_failure(self, redis, monkeypatch):
        """A blocked job failed by its dependency is terminal and will never
        run, so its cancel flag has no reader left."""
        from app.queue import queue

        await redis.sadd("bp:cancel", "blocked1")
        monkeypatch.setattr(queue, "get_redis", lambda: redis)

        await queue._clear_cancel_flag("blocked1")

        assert await redis.sismember("bp:cancel", "blocked1") == 0

    async def test_is_safe_when_no_flag_was_set(self, redis, monkeypatch):
        from app.queue import queue

        monkeypatch.setattr(queue, "get_redis", lambda: redis)
        await queue._clear_cancel_flag("never-cancelled")
        assert await redis.sismember("bp:cancel", "never-cancelled") == 0

    async def test_survives_a_redis_outage(self, monkeypatch):
        """Cleanup is hygiene, not correctness -- it must never fail a job."""
        from app.queue import queue

        class Boom:
            async def srem(self, *a, **kw):
                raise ConnectionError("redis is down")

        monkeypatch.setattr(queue, "get_redis", lambda: Boom())
        await queue._clear_cancel_flag("job1")  # must not raise
