"""Lease expiry, release, promotion, and periodic-schedule ticks."""

import asyncio

from tests.queue.conftest import ALL_CLASSES

NOW_MS = 1_767_300_000_000
LEASE_MS = 30_000


async def claim(scripts, *, worker="w1", classes=ALL_CLASSES, cpu=8, mem=8192, io=2,
                now=NOW_MS):
    return await scripts["claim"](
        keys=["bp:q:ready", "bp:q:running"],
        args=[now, LEASE_MS, worker, classes, cpu, mem, io, 50],
    )


class TestRelease:
    async def test_frees_reserved_resources(self, redis, scripts, job_factory):
        await job_factory("job1", cpu=4, mem_mb=2048, io="heavy")
        await claim(scripts)
        assert int(await redis.get("bp:conc:cpu")) == 4

        released = await scripts["release"](
            keys=["bp:q:running", "bp:q:ready"], args=["job1", "0", 0]
        )
        assert released == 1
        assert int(await redis.get("bp:conc:cpu")) == 0
        assert int(await redis.get("bp:conc:mem_mb")) == 0
        assert int(await redis.get("bp:conc:io_heavy")) == 0

    async def test_is_idempotent(self, redis, scripts, job_factory):
        """Releasing twice must not double-decrement: the counters would drift
        negative and the queue would over-admit forever."""
        await job_factory("job1", cpu=4)
        await claim(scripts)

        assert await scripts["release"](keys=["bp:q:running", "bp:q:ready"],
                                        args=["job1", "0", 0]) == 1
        assert await scripts["release"](keys=["bp:q:running", "bp:q:ready"],
                                        args=["job1", "0", 0]) == 0
        assert int(await redis.get("bp:conc:cpu")) == 0

    async def test_requeue_puts_the_job_back(self, redis, scripts, job_factory):
        await job_factory("job1")
        await claim(scripts)

        await scripts["release"](keys=["bp:q:running", "bp:q:ready"],
                                 args=["job1", "1", 5000])

        assert await redis.zscore("bp:q:ready", "job1") == 5000
        assert await redis.zscore("bp:q:running", "job1") is None
        # Dispatch metadata survives a requeue so the job can be claimed again.
        assert await redis.hget("bp:job:job1", "class") is not None

    async def test_drop_clears_metadata_and_cancel_flag(self, redis, scripts, job_factory):
        await job_factory("job1")
        await claim(scripts)
        await redis.sadd("bp:cancel", "job1")

        await scripts["release"](keys=["bp:q:running", "bp:q:ready"],
                                 args=["job1", "0", 0])

        assert await redis.exists("bp:job:job1") == 0
        assert await redis.sismember("bp:cancel", "job1") == 0


class TestReapExpired:
    async def test_requeues_an_expired_lease(self, redis, scripts, job_factory):
        """The laptop-lid case: the VM paused, the lease lapsed, but the job is
        still perfectly valid work."""
        await job_factory("job1", score=1234)
        await claim(scripts)

        result = await scripts["reap_expired"](
            keys=["bp:q:running", "bp:q:ready"],
            args=[NOW_MS + LEASE_MS + 1, 100],
        )

        assert result[0] == "job1"
        assert int(result[1]) == 1  # attempts incremented
        assert await redis.zscore("bp:q:ready", "job1") == 1234  # original priority kept
        assert await redis.zscore("bp:q:running", "job1") is None

    async def test_does_not_touch_a_live_lease(self, redis, scripts, job_factory):
        await job_factory("job1")
        await claim(scripts)

        result = await scripts["reap_expired"](
            keys=["bp:q:running", "bp:q:ready"], args=[NOW_MS + 1000, 100]
        )
        assert result == []
        assert await redis.zscore("bp:q:running", "job1") is not None

    async def test_releases_resources_of_reaped_jobs(self, redis, scripts, job_factory):
        await job_factory("job1", cpu=4, mem_mb=1024, io="heavy")
        await claim(scripts)

        await scripts["reap_expired"](
            keys=["bp:q:running", "bp:q:ready"], args=[NOW_MS + LEASE_MS + 1, 100]
        )

        assert int(await redis.get("bp:conc:cpu")) == 0
        assert int(await redis.get("bp:conc:io_heavy")) == 0

    async def test_reaped_job_gets_a_fresh_epoch_on_reclaim(self, redis, scripts, job_factory):
        """Fencing: the original worker holds epoch 1, so its write-backs are
        rejected once epoch 2 has been granted."""
        await job_factory("job1")
        first = await claim(scripts, worker="w1")

        await scripts["reap_expired"](
            keys=["bp:q:running", "bp:q:ready"], args=[NOW_MS + LEASE_MS + 1, 100]
        )
        second = await claim(scripts, worker="w2", now=NOW_MS + LEASE_MS + 2)

        assert int(first[5]) == 1
        assert int(second[5]) == 2

    async def test_batch_limit_is_respected(self, redis, scripts, job_factory):
        for i in range(10):
            await job_factory(f"job{i}", score=1000 + i)
            await claim(scripts, worker=f"w{i}")

        result = await scripts["reap_expired"](
            keys=["bp:q:running", "bp:q:ready"], args=[NOW_MS + LEASE_MS + 1, 3]
        )
        assert len(result) == 6  # 3 pairs of (job_id, attempts)


class TestPromoteDelayed:
    async def test_moves_due_jobs_to_ready(self, redis, scripts):
        await redis.hset("bp:job:job1", mapping={"score": 1500, "class": "user_background"})
        await redis.zadd("bp:q:delayed", {"job1": NOW_MS - 1})

        moved = await scripts["promote_delayed"](
            keys=["bp:q:delayed", "bp:q:ready"], args=[NOW_MS, 100]
        )

        assert moved == ["job1"]
        assert await redis.zscore("bp:q:ready", "job1") == 1500

    async def test_leaves_future_jobs_alone(self, redis, scripts):
        await redis.hset("bp:job:job1", mapping={"score": 1500})
        await redis.zadd("bp:q:delayed", {"job1": NOW_MS + 60_000})

        moved = await scripts["promote_delayed"](
            keys=["bp:q:delayed", "bp:q:ready"], args=[NOW_MS, 100]
        )
        assert moved == []
        assert await redis.zscore("bp:q:delayed", "job1") is not None


class TestPromoteAged:
    async def test_promotes_a_starving_job_into_the_next_tier(self, redis, scripts,
                                                              job_factory):
        """Without this, sustained user load means maintenance never runs -- and
        a verify_files job that never runs fails silently."""
        from app.models import JobClass
        from app.queue.priority import BASE_SCORES

        maint_base = BASE_SCORES[JobClass.MAINTENANCE]
        target_base = BASE_SCORES[JobClass.USER_BACKGROUND]

        await job_factory("old", job_class="maintenance", score=maint_base + 100)
        await job_factory("new", job_class="maintenance", score=maint_base + 5000)

        promoted = await scripts["promote_aged"](
            keys=["bp:q:ready"],
            args=[maint_base + 1000, maint_base, target_base, 200],
        )

        assert promoted == 1
        assert await redis.zscore("bp:q:ready", "old") == target_base + 100
        assert await redis.zscore("bp:q:ready", "new") == maint_base + 5000

    async def test_preserves_relative_age_among_promoted_jobs(self, redis, scripts,
                                                              job_factory):
        from app.models import JobClass
        from app.queue.priority import BASE_SCORES

        base = BASE_SCORES[JobClass.MAINTENANCE]
        target = BASE_SCORES[JobClass.USER_BACKGROUND]
        await job_factory("older", job_class="maintenance", score=base + 10)
        await job_factory("newer", job_class="maintenance", score=base + 20)

        await scripts["promote_aged"](
            keys=["bp:q:ready"], args=[base + 1000, base, target, 200]
        )

        assert await redis.zscore("bp:q:ready", "older") < await redis.zscore(
            "bp:q:ready", "newer"
        )

    async def test_does_not_promote_other_classes(self, redis, scripts, job_factory):
        from app.models import JobClass
        from app.queue.priority import BASE_SCORES

        maint_base = BASE_SCORES[JobClass.MAINTENANCE]
        bulk_score = BASE_SCORES[JobClass.BULK] + 50
        await job_factory("bulk1", job_class="bulk", score=bulk_score)

        await scripts["promote_aged"](
            keys=["bp:q:ready"],
            args=[maint_base + 1000, maint_base, BASE_SCORES[JobClass.USER_BACKGROUND], 200],
        )
        assert await redis.zscore("bp:q:ready", "bulk1") == bulk_score


class TestScheduleTick:
    async def test_first_call_arms_without_firing(self, redis, scripts):
        """A restart must not fire every schedule at once."""
        fired = await scripts["schedule_tick"](
            keys=["bp:sched:next:verify"], args=[NOW_MS, 60_000, "0"]
        )
        assert fired == 0
        assert int(await redis.get("bp:sched:next:verify")) == NOW_MS + 60_000

    async def test_fires_when_due(self, redis, scripts):
        await redis.set("bp:sched:next:verify", NOW_MS - 1)
        assert await scripts["schedule_tick"](
            keys=["bp:sched:next:verify"], args=[NOW_MS, 60_000, "0"]
        ) == 1

    async def test_does_not_fire_early(self, redis, scripts):
        await redis.set("bp:sched:next:verify", NOW_MS + 30_000)
        assert await scripts["schedule_tick"](
            keys=["bp:sched:next:verify"], args=[NOW_MS, 60_000, "0"]
        ) == 0

    async def test_exactly_one_worker_wins_each_tick(self, redis, scripts):
        """Five workers tick simultaneously; a duplicate would mean the same
        maintenance job enqueued repeatedly."""
        await redis.set("bp:sched:next:verify", NOW_MS - 1)

        results = await asyncio.gather(
            *(
                scripts["schedule_tick"](
                    keys=["bp:sched:next:verify"], args=[NOW_MS, 60_000, "0"]
                )
                for _ in range(5)
            )
        )
        assert sum(results) == 1

    async def test_no_catchup_collapses_a_long_sleep_into_one_tick(self, redis, scripts):
        """A four-hour laptop sleep must produce one tick, not 240."""
        await redis.set("bp:sched:next:verify", NOW_MS - 4 * 3600 * 1000)

        fired = await scripts["schedule_tick"](
            keys=["bp:sched:next:verify"], args=[NOW_MS, 60_000, "0"]
        )
        assert fired == 1
        # Next run is relative to now, so the backlog is discarded.
        assert int(await redis.get("bp:sched:next:verify")) == NOW_MS + 60_000

    async def test_catchup_mode_advances_one_interval_at_a_time(self, redis, scripts):
        await redis.set("bp:sched:next:verify", NOW_MS - 120_000)
        fired = await scripts["schedule_tick"](
            keys=["bp:sched:next:verify"], args=[NOW_MS, 60_000, "1"]
        )
        assert fired == 1
        assert int(await redis.get("bp:sched:next:verify")) == NOW_MS - 60_000

    async def test_repeated_ticks_over_time_fire_once_per_interval(self, redis, scripts):
        await redis.set("bp:sched:next:verify", NOW_MS)
        fired = 0
        # Poll every 10s of simulated time across 5 minutes.
        for step in range(30):
            fired += await scripts["schedule_tick"](
                keys=["bp:sched:next:verify"],
                args=[NOW_MS + step * 10_000, 60_000, "0"],
            )
        assert fired == 5  # 300s / 60s
