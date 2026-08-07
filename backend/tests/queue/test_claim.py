"""Atomic claim semantics -- the core correctness property of the queue."""

import asyncio

import pytest

from tests.queue.conftest import ALL_CLASSES

NOW_MS = 1_767_300_000_000
LEASE_MS = 30_000


async def claim(
    scripts,
    *,
    worker="w1",
    classes=ALL_CLASSES,
    cpu=8,
    mem=8192,
    io=2,
    limit=50,
    ignore_reservations=False,
):
    """cpu/mem/io are budgets, not precomputed free amounts: claim.lua reads
    the live bp:conc:* counters itself and subtracts them from these ceilings
    as part of its own atomic execution."""
    return await scripts["claim"](
        keys=["bp:q:ready", "bp:q:running"],
        args=[
            NOW_MS,
            LEASE_MS,
            worker,
            classes,
            cpu,
            mem,
            io,
            limit,
            "1" if ignore_reservations else "0",
        ],
    )


class TestBasicClaim:
    async def test_claims_a_ready_job(self, redis, scripts, job_factory):
        await job_factory("job1")
        result = await claim(scripts)

        assert result is not None
        assert result[0] == "job1"
        # Moved out of ready and into running with a lease.
        assert await redis.zscore("bp:q:ready", "job1") is None
        assert await redis.zscore("bp:q:running", "job1") == NOW_MS + LEASE_MS

    async def test_returns_nil_when_queue_is_empty(self, scripts):
        assert await claim(scripts) is None

    async def test_records_the_claiming_worker(self, redis, scripts, job_factory):
        await job_factory("job1")
        await claim(scripts, worker="worker-7")
        assert await redis.hget("bp:job:job1", "worker_id") == "worker-7"

    async def test_reserves_declared_resources(self, redis, scripts, job_factory):
        await job_factory("job1", cpu=4, mem_mb=2048, io="heavy")
        await claim(scripts)

        assert int(await redis.get("bp:conc:cpu")) == 4
        assert int(await redis.get("bp:conc:mem_mb")) == 2048
        assert int(await redis.get("bp:conc:io_heavy")) == 1


class TestPriorityOrdering:
    async def test_dispatches_highest_priority_first(self, scripts, job_factory):
        """A user-initiated job must preempt queued maintenance work."""
        await job_factory("bulk1", job_class="bulk")
        await job_factory("maint1", job_class="maintenance")
        await job_factory("user1", job_class="user_interactive")

        assert (await claim(scripts))[0] == "user1"
        assert (await claim(scripts))[0] == "maint1"
        assert (await claim(scripts))[0] == "bulk1"

    async def test_fifo_within_a_class(self, scripts, job_factory):
        await job_factory("first", score=1_000_100)
        await job_factory("second", score=1_000_200)
        assert (await claim(scripts))[0] == "first"
        assert (await claim(scripts))[0] == "second"


class TestAdmissionControl:
    async def test_skips_classes_the_governor_excludes(self, scripts, job_factory):
        """Under strain the governor admits only user work; maintenance waits
        rather than being dropped."""
        await job_factory("maint1", job_class="maintenance")
        await job_factory("user1", job_class="user_interactive")

        result = await claim(scripts, classes="user_interactive")
        assert result[0] == "user1"
        assert await claim(scripts, classes="user_interactive") is None

    async def test_maintenance_remains_queued_when_excluded(self, redis, scripts, job_factory):
        await job_factory("maint1", job_class="maintenance")
        await claim(scripts, classes="user_interactive")
        assert await redis.zscore("bp:q:ready", "maint1") is not None


class TestResourceGating:
    async def test_skips_jobs_that_do_not_fit_cpu(self, scripts, job_factory):
        await job_factory("big", cpu=16, score=100)
        await job_factory("small", cpu=1, score=200)

        result = await claim(scripts, cpu=4)
        assert result[0] == "small"  # the higher-priority job did not fit

    async def test_skips_jobs_that_do_not_fit_memory(self, scripts, job_factory):
        await job_factory("hungry", mem_mb=16000, score=100)
        await job_factory("modest", mem_mb=128, score=200)

        assert (await claim(scripts, mem=1024))[0] == "modest"

    async def test_respects_the_heavy_io_cap(self, scripts, job_factory):
        """More than two concurrent heavy readers on a FUSE mount is slower in
        aggregate, so the cap is a throughput guard as much as a safety one."""
        await job_factory("io1", io="heavy", score=100)
        await job_factory("io2", io="heavy", score=200)

        assert (await claim(scripts, io=1))[0] == "io1"
        assert await claim(scripts, io=0) is None

    async def test_zero_cost_jobs_always_fit(self, scripts, job_factory):
        await job_factory("free", cpu=0, mem_mb=0)
        assert (await claim(scripts, cpu=0, mem=0))[0] == "free"


class TestLiveReservationRead:
    """The fix for #74: claim.lua reads bp:conc:* live instead of trusting a
    caller-supplied free value computed moments earlier."""

    async def test_a_reservation_written_after_the_caller_computed_its_budget_is_still_honoured(
        self, redis, scripts, job_factory
    ):
        """The caller's budget argument is the raw ceiling (8192), same as if
        nothing were reserved yet. If claim.lua trusted that number instead of
        reading the counter itself, this job would wrongly be admitted."""
        await job_factory("job1", mem_mb=6144)
        await redis.set("bp:conc:mem_mb", 6000)  # reserved by "another worker" after budgeting

        assert await claim(scripts, mem=8192) is None

    async def test_ignore_reservations_skips_the_live_read(self, redis, scripts, job_factory):
        """The in-flight self-healing clamp: an idle worker's own reservations
        cannot still be outstanding, so it is told to disregard the counter
        entirely rather than have it read (correctly) as leaked capacity."""
        await job_factory("job1", mem_mb=6144)
        await redis.set("bp:conc:mem_mb", 99999)

        result = await claim(scripts, mem=8192, ignore_reservations=True)
        assert result is not None
        assert result[0] == "job1"

    async def test_a_negative_counter_is_not_read_as_extra_capacity(
        self, redis, scripts, job_factory
    ):
        await job_factory("job1", mem_mb=100)
        await redis.set("bp:conc:mem_mb", -50)

        result = await claim(scripts, mem=0)
        assert result is None, "a negative counter must clamp to zero reserved, not add headroom"


class TestExactlyOnceClaiming:
    async def test_concurrent_workers_never_double_claim(self, redis, scripts, job_factory):
        """The property the whole Lua script exists for.

        Twenty workers race for ten jobs; every job must be claimed exactly
        once. Doing select-then-reserve in separate round trips would let two
        workers observe the same free capacity and both win.
        """
        for i in range(10):
            await job_factory(f"job{i}", score=1000 + i)

        results = await asyncio.gather(
            *(claim(scripts, worker=f"w{i}") for i in range(20))
        )

        claimed = [r[0] for r in results if r is not None]
        assert len(claimed) == 10, f"expected 10 claims, got {len(claimed)}"
        assert len(set(claimed)) == 10, "a job was claimed more than once"
        assert await redis.zcard("bp:q:ready") == 0
        assert await redis.zcard("bp:q:running") == 10

    async def test_resource_counters_stay_consistent_under_contention(
        self, redis, scripts, job_factory
    ):
        for i in range(10):
            await job_factory(f"job{i}", cpu=1, mem_mb=100, score=1000 + i)

        await asyncio.gather(*(claim(scripts, worker=f"w{i}") for i in range(20)))

        # Exactly one reservation per claimed job, no double counting.
        assert int(await redis.get("bp:conc:cpu")) == 10
        assert int(await redis.get("bp:conc:mem_mb")) == 1000


class TestFencingToken:
    async def test_epoch_increments_on_every_grant(self, redis, scripts, job_factory):
        """The guard against a paused VM resuming and writing over a job that
        another worker has since taken over."""
        await job_factory("job1", epoch=0)

        first = await claim(scripts, worker="w1")
        assert int(first[5]) == 1

        # Simulate lease expiry: the job returns to ready.
        await redis.zrem("bp:q:running", "job1")
        await redis.zadd("bp:q:ready", {"job1": 1000})

        second = await claim(scripts, worker="w2")
        assert int(second[5]) == 2, "epoch must advance so stale writes are rejected"


class TestRobustness:
    async def test_drops_queued_ids_with_no_metadata(self, redis, scripts):
        """Redis lost the hash but kept the id. Mongo is the record of truth, so
        the orphan is discarded rather than dispatched blindly."""
        await redis.zadd("bp:q:ready", {"orphan": 100})
        assert await claim(scripts) is None
        assert await redis.zscore("bp:q:ready", "orphan") is None

    async def test_bounded_scan_does_not_starve_on_a_deep_queue(
        self, redis, scripts, job_factory
    ):
        """Only the top N are scanned; anything deeper is lower priority anyway,
        so waiting for the next tick costs nothing."""
        for i in range(60):
            await job_factory(f"big{i}", cpu=99, score=1000 + i)
        await job_factory("fits", cpu=1, score=9999)

        assert await claim(scripts, cpu=4, limit=50) is None

        # Once the blockers clear, it dispatches.
        for i in range(60):
            await redis.zrem("bp:q:ready", f"big{i}")
        assert (await claim(scripts, cpu=4, limit=50))[0] == "fits"


@pytest.mark.parametrize(
    "job_class", ["user_interactive", "user_background", "maintenance", "bulk"]
)
async def test_every_class_is_claimable_when_admitted(scripts, job_factory, job_class):
    await job_factory("job1", job_class=job_class)
    result = await claim(scripts)
    assert result is not None
    assert result[1] == job_class
