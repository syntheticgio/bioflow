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
