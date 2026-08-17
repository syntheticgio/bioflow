"""Per-node claim routing: jobs enqueued on a node-specific queue are claimed
only by workers on that node, and use per-node concurrency counters."""


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
    node_id="",
    ready_key="bp:q:ready",
):
    """Call claim.lua with per-node args."""
    return await scripts["claim"](
        keys=[ready_key, "bp:q:running"],
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
            node_id,
        ],
    )


async def _make_node_job(redis, job_id, *, node_id, score=1000):
    """Insert a job into a node-specific ready queue (as _push_to_redis does)."""
    await redis.hset(
        f"bp:job:{job_id}",
        mapping={
            "type": "noop",
            "class": "user_background",
            "cpu": 1,
            "mem_mb": 128,
            "io": "none",
            "attempts": 0,
            "score": score,
            "epoch": 0,
            "override": "0",
            "node": node_id,
        },
    )
    ready_key = f"bp:q:ready:{node_id}"
    await redis.zadd(ready_key, {job_id: score})


class TestPerNodeClaimRouting:
    async def test_claims_from_node_specific_queue(self, redis, scripts):
        """A worker on gpu-node claims a job from bp:q:ready:gpu-node."""
        await _make_node_job(redis, "job1", node_id="gpu-node")

        result = await claim(
            scripts, node_id="gpu-node", ready_key="bp:q:ready:gpu-node"
        )
        assert result is not None
        assert result[0] == "job1"
        assert await redis.zscore("bp:q:ready:gpu-node", "job1") is None
        assert await redis.zscore("bp:q:running", "job1") == NOW_MS + LEASE_MS

    async def test_node_specific_job_is_not_visible_to_global_pool(
        self, redis, scripts
    ):
        """A global-pool worker cannot claim a job enqueued on a node queue."""
        await _make_node_job(redis, "job1", node_id="gpu-node")

        result = await claim(scripts, node_id="", ready_key="bp:q:ready")
        assert result is None

    async def test_global_job_is_not_visible_to_node_pool(self, redis, scripts, job_factory):
        """A node worker doesn't steal jobs from the global pool."""
        await job_factory("job1")

        result = await claim(
            scripts, node_id="gpu-node", ready_key="bp:q:ready:gpu-node"
        )
        assert result is None  # job1 is in bp:q:ready, not the node queue


class TestPerNodeConcurrencyCounters:
    async def test_increments_per_node_counters(self, redis, scripts):
        await _make_node_job(redis, "job1", node_id="gpu-node")

        # Set specific resource demands on the job hash.
        await redis.hset(
            "bp:job:job1",
            mapping={"cpu": 4, "mem_mb": 2048, "io": "heavy"},
        )

        await claim(scripts, node_id="gpu-node", ready_key="bp:q:ready:gpu-node")

        # Per-node counters.
        assert int(await redis.get("bp:conc:cpu:gpu-node")) == 4
        assert int(await redis.get("bp:conc:mem_mb:gpu-node")) == 2048
        assert int(await redis.get("bp:conc:io_heavy:gpu-node")) == 1

        # Global counters untouched.
        assert int(await redis.get("bp:conc:cpu") or 0) == 0
        assert int(await redis.get("bp:conc:mem_mb") or 0) == 0

    async def test_global_counters_still_work_without_node_id(self, redis, scripts, job_factory):
        """Backward compat: no node_id → global counters."""
        await job_factory("job1", cpu=4, mem_mb=2048)

        await claim(scripts, node_id="", ready_key="bp:q:ready")

        assert int(await redis.get("bp:conc:cpu")) == 4
        assert int(await redis.get("bp:conc:mem_mb")) == 2048

    async def test_per_node_counters_are_independent(self, redis, scripts):
        """Reservations on gpu-node don't block claims on cpu-node."""
        await _make_node_job(redis, "job1", node_id="gpu-node")
        await _make_node_job(redis, "job2", node_id="cpu-node")

        # Saturate gpu-node's CPU budget.
        await claim(scripts, node_id="gpu-node", ready_key="bp:q:ready:gpu-node")
        assert int(await redis.get("bp:conc:cpu:gpu-node")) == 1

        # cpu-node should still be able to claim.
        result = await claim(
            scripts,
            node_id="cpu-node",
            ready_key="bp:q:ready:cpu-node",
            cpu=8,
        )
        assert result is not None
        assert result[0] == "job2"


class TestReleasePerNodeCounters:
    async def test_release_decrements_per_node_counters(self, redis, scripts):
        """release.lua reads the `node` field from the job hash and decrements
        the correct per-node counters."""
        await _make_node_job(redis, "job1", node_id="gpu-node")
        await claim(scripts, node_id="gpu-node", ready_key="bp:q:ready:gpu-node")

        # Release the job via release.lua.
        await scripts["release"](
            keys=["bp:q:running", "bp:q:ready"],
            args=["job1", "0", "0"],  # job_id, requeue=0, score=0
        )

        assert int(await redis.get("bp:conc:cpu:gpu-node")) == 0
        assert int(await redis.get("bp:conc:mem_mb:gpu-node")) == 0


class TestReapExpiredPerNode:
    async def test_reap_decrements_per_node_counters(self, redis, scripts):
        """reap_expired.lua reads `node` and decrements per-node counters."""
        await _make_node_job(redis, "job1", node_id="gpu-node")

        # Simulate a claim + expired lease: move to running with an old score,
        # and set per-node concurrency counters as claim.lua would.
        await redis.hset("bp:job:job1", key="node", value="gpu-node")
        await redis.zadd("bp:q:running", {"job1": NOW_MS - 1000})
        await redis.set("bp:conc:cpu:gpu-node", 1)
        await redis.set("bp:conc:mem_mb:gpu-node", 128)

        # Now reap.
        await scripts["reap_expired"](
            keys=["bp:q:running", "bp:q:ready"],
            args=[NOW_MS, 10],
        )

        assert int(await redis.get("bp:conc:cpu:gpu-node")) == 0
        assert int(await redis.get("bp:conc:mem_mb:gpu-node")) == 0


class TestWorkerClaimQueueOrdering:
    async def test_node_queue_tried_before_global(self, redis, scripts, job_factory):
        """The worker tries node-specific first, then global. A job with target_node
        is claimed from its node queue, not the global pool."""
        await _make_node_job(redis, "node_job", node_id="primary")
        await job_factory("global_job")

        # First claim from node queue.
        node_result = await claim(
            scripts, node_id="primary", ready_key="bp:q:ready:primary"
        )
        assert node_result is not None
        assert node_result[0] == "node_job"

        # Global claim still works independently.
        global_result = await claim(scripts, node_id="", ready_key="bp:q:ready")
        assert global_result is not None
        assert global_result[0] == "global_job"
