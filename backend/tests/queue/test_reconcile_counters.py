"""`reconcile()` rebuilds the `bp:conc:*` reservation counters from truth.

The counters are the *only* admission gate claim.lua consults, and several
paths (a cancel racing a claim, a non-idempotent retry, a hard crash between
INCRBY and the matching release) can leave an increment that no release ever
reverses. Nothing zeroes the counters, so such a leak is permanent until Redis
is flushed -- a phantom reservation that shrinks headroom forever and, once it
drives the memory counter above the budget, silently refuses every future job.

These tests assert the direction that fails when the rebuild is missing: a
corrupted counter is restored to the true sum over the live RUNNING set, not
merely left as-is. The state is built by claiming real jobs through the actual
Lua scripts (fakeredis runs them), so the "truth" the rebuild must reach is the
truth claim.lua itself wrote.
"""

import pytest
from beanie import init_beanie
from pymongo import AsyncMongoClient

from app.config import settings
from app.models import ALL_MODELS
from app.models.job import JobResources
from tests._mongo_isolation import direct_mongo_url, worker_db_name


@pytest.fixture(autouse=True)
async def _init_beanie_models(monkeypatch):
    """`reconcile()` reads jobs through Beanie, so it needs a real database.

    Same function-scoped pattern as test_resource_override.py: this suite does
    I/O and pytest-asyncio hands each async test its own event loop.
    """
    client = AsyncMongoClient(direct_mongo_url(settings.mongo_url), tz_aware=True)
    db = client[worker_db_name()]
    await init_beanie(database=db, document_models=ALL_MODELS)
    monkeypatch.setattr("app.db.client.get_db", lambda: db)
    yield
    await client.close()


async def _claim(queue, node_id="", **kwargs):
    """Claim the best job against a generous budget, everything else default."""
    return await queue.claim(
        "w1",
        allowed_classes=["user_background"],
        cpu_budget=64,
        mem_mb_budget=64000,
        io_heavy_budget=8,
        node_id=node_id,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_reconcile_repairs_a_leaked_global_counter(redis, scripts, monkeypatch):
    """A phantom increment no release reversed is corrected to the true sum.

    Claiming the job drives the counters to the honest value through claim.lua;
    we then add a leak on top and prove reconcile subtracts it back out rather
    than trusting the corrupted number.
    """
    from app.queue import keys, queue

    monkeypatch.setattr(queue, "get_redis", lambda: redis)
    monkeypatch.setattr(queue, "get_script", lambda name: scripts[name])

    job = await queue.enqueue(
        "align_reads", owner="p1", resources=JobResources(cpu=4, mem_mb=2000)
    )
    assert job is not None
    claimed = await _claim(queue)
    assert claimed is not None  # now RUNNING; counters hold cpu=4, mem_mb=2000

    # A leak: an increment whose matching release was lost (the cancel-race and
    # non-idempotent-retry classes both produce exactly this shape).
    await redis.incrby(keys.conc_key("mem_mb"), 5000)
    await redis.incrby(keys.conc_key("cpu"), 3)

    await queue.reconcile()

    # Rebuilt to the one live RUNNING job's declared demand, leak discarded.
    assert int(await redis.get(keys.conc_key("mem_mb"))) == 2000
    assert int(await redis.get(keys.conc_key("cpu"))) == 4


@pytest.mark.asyncio
async def test_reconcile_zeroes_a_counter_with_nothing_running(
    redis, scripts, monkeypatch
):
    """A counter left non-zero after every job finished must return to zero.

    This is the failure that starves the queue: a stuck positive mem_mb counter
    with an empty RUNNING set refuses jobs that the machine has ample room for.
    """
    from app.queue import keys, queue

    monkeypatch.setattr(queue, "get_redis", lambda: redis)
    monkeypatch.setattr(queue, "get_script", lambda name: scripts[name])

    # Nothing is running, yet a stale reservation lingers.
    await redis.set(keys.conc_key("mem_mb"), 4096)
    await redis.set(keys.conc_key("cpu"), 8)
    await redis.set(keys.conc_key("io_heavy"), 1)

    await queue.reconcile()

    # An absent counter reads as zero to claim.lua (`tonumber(nil) or 0`), so
    # either "0" or a deleted key is correct; assert the value claim.lua sees.
    assert int(await redis.get(keys.conc_key("mem_mb")) or 0) == 0
    assert int(await redis.get(keys.conc_key("cpu")) or 0) == 0
    assert int(await redis.get(keys.conc_key("io_heavy")) or 0) == 0


@pytest.mark.asyncio
async def test_reconcile_counts_io_heavy_and_scopes_per_node(
    redis, scripts, monkeypatch
):
    """The rebuild must honour both the io=='heavy' rule and node scoping.

    io_heavy is a count of heavy jobs, not a sum of a field, and the counters
    are per-node when a job was claimed on a node. A rebuild that ignored
    either would pass the single-global-job test above yet corrupt a real
    multi-node install.
    """
    from app.queue import keys, queue

    monkeypatch.setattr(queue, "get_redis", lambda: redis)
    monkeypatch.setattr(queue, "get_script", lambda name: scripts[name])

    # A heavy job pinned to a node. enqueue targets the node's ready queue;
    # claim reserves against that node's counters.
    node = "node2"
    job = await queue.enqueue(
        "align_reads",
        owner="p1",
        resources=JobResources(cpu=2, mem_mb=1500, io="heavy"),
        target_node=node,
    )
    assert job is not None
    claimed = await _claim(queue, node_id=node, ready_key=keys.ready_key(node))
    assert claimed is not None

    # Corrupt the per-node counters.
    await redis.incrby(keys.conc_key("mem_mb", node), 9999)
    await redis.set(keys.conc_key("io_heavy", node), 7)

    await queue.reconcile()

    assert int(await redis.get(keys.conc_key("mem_mb", node))) == 1500
    assert int(await redis.get(keys.conc_key("cpu", node))) == 2
    assert int(await redis.get(keys.conc_key("io_heavy", node))) == 1
    # The global counters were never touched by a node-scoped claim.
    assert int(await redis.get(keys.conc_key("mem_mb")) or 0) == 0


@pytest.mark.asyncio
async def test_reconcile_leaves_honest_counters_untouched(
    redis, scripts, monkeypatch
):
    """The rebuild is idempotent: a correct counter survives it unchanged.

    Guards against a rebuild that, say, double-counts or drops the live job --
    running reconcile on already-correct state must be a no-op on the counters.
    """
    from app.queue import keys, queue

    monkeypatch.setattr(queue, "get_redis", lambda: redis)
    monkeypatch.setattr(queue, "get_script", lambda name: scripts[name])

    job = await queue.enqueue(
        "align_reads", owner="p1", resources=JobResources(cpu=4, mem_mb=2000)
    )
    assert job is not None
    assert await _claim(queue) is not None

    assert int(await redis.get(keys.conc_key("mem_mb"))) == 2000

    await queue.reconcile()

    assert int(await redis.get(keys.conc_key("mem_mb"))) == 2000
    assert int(await redis.get(keys.conc_key("cpu"))) == 4
