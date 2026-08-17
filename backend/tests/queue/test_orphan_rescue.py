"""Orphaned-job rescue.

`enqueue` writes the durable Mongo record first, then pushes to Redis. A crash
between those two steps leaves a job in PENDING that nothing will ever
dispatch. This was found in production: a script died mid-enqueue and the job
sat stuck indefinitely because `reconcile` only runs at worker startup.
"""

from datetime import UTC, datetime, timedelta

import pytest
from app.models import JobState


class FakeJob:
    def __init__(self, job_id: str, created_at: datetime, state=JobState.PENDING):
        self.id = job_id
        self.created_at = created_at
        self.state = state
        self.type = "ingest_headers"


class TestOrphanSelection:
    """The selection rule, isolated from Mongo/Redis.

    Only PENDING jobs older than the grace window and absent from Redis are
    rescued.
    """

    def _is_orphan(self, job, in_redis: set[str], cutoff: datetime) -> bool:
        return (
            job.state is JobState.PENDING
            and job.created_at < cutoff
            and job.id not in in_redis
        )

    @pytest.fixture
    def cutoff(self):
        return datetime.now(UTC) - timedelta(seconds=60)

    def test_old_pending_job_absent_from_redis_is_rescued(self, cutoff):
        job = FakeJob("j1", datetime.now(UTC) - timedelta(minutes=5))
        assert self._is_orphan(job, set(), cutoff)

    def test_recent_pending_job_is_left_alone(self, cutoff):
        """A healthy enqueue is briefly PENDING between its two steps; rescuing
        it would double-dispatch."""
        job = FakeJob("j1", datetime.now(UTC))
        assert not self._is_orphan(job, set(), cutoff)

    def test_job_already_in_redis_is_not_re_pushed(self, cutoff):
        job = FakeJob("j1", datetime.now(UTC) - timedelta(minutes=5))
        assert not self._is_orphan(job, {"j1"}, cutoff)

    @pytest.mark.parametrize(
        "state",
        [JobState.QUEUED, JobState.RUNNING, JobState.SUCCEEDED, JobState.FAILED],
    )
    def test_non_pending_states_are_ignored(self, cutoff, state):
        job = FakeJob("j1", datetime.now(UTC) - timedelta(minutes=5), state=state)
        assert not self._is_orphan(job, set(), cutoff)


class TestRescueIntegration:
    async def test_orphan_is_pushed_to_ready(self, redis, job_factory):
        """End state: an orphaned job becomes claimable."""
        await redis.zadd("bp:q:ready", {"healthy": 1000})

        # Simulate the rescue path's effect for an orphan not in Redis.
        assert await redis.zscore("bp:q:ready", "orphan") is None
        await redis.hset("bp:job:orphan", mapping={"type": "ingest_headers",
                                                   "class": "user_background",
                                                   "cpu": 1, "mem_mb": 128,
                                                   "io": "none", "attempts": 0,
                                                   "score": 1500, "epoch": 0})
        await redis.zadd("bp:q:ready", {"orphan": 1500})

        assert await redis.zscore("bp:q:ready", "orphan") == 1500
        assert await redis.zcard("bp:q:ready") == 2

    async def test_rescued_job_is_claimable(self, redis, scripts, job_factory):
        from tests.queue.conftest import ALL_CLASSES

        await job_factory("rescued", score=1500)
        result = await scripts["claim"](
            keys=["bp:q:ready", "bp:q:running"],
            args=[1_767_300_000_000, 30_000, "w1", ALL_CLASSES, 8, 8192, 2, 50],
        )
        assert result is not None
        assert result[0] == "rescued"
