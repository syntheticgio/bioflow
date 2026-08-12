"""The launch-anyway override, from the job document through to claim.lua.

The assertions here are chosen for the direction that fails when the seam
breaks. Asserting that an overridden job IS claimed proves little -- most
things are claimable in a quiet test environment. Asserting it is REFUSED
under contention is what fails if `sole` is computed wrongly.
"""

import pytest
from beanie import init_beanie
from pymongo import AsyncMongoClient

from app.config import settings
from app.models import ALL_MODELS
from app.models.job import Job, JobState


@pytest.fixture(autouse=True)
async def _init_beanie_models(monkeypatch):
    """`Job.insert`/`Job.get` need Beanie initialized against a real database --
    same pattern as `tests/queue/test_cancel_cleanup.py`. Function-scoped since
    this suite performs I/O and pytest-asyncio hands each async test its own
    event loop by default.
    """
    client = AsyncMongoClient(settings.mongo_url, tz_aware=True)
    db = client["biopipe_test"]
    await init_beanie(database=db, document_models=ALL_MODELS)
    monkeypatch.setattr("app.db.client.get_db", lambda: db)
    yield
    await client.close()


@pytest.mark.asyncio
async def test_resource_override_defaults_to_false():
    job = Job(type="align_reads", owner="p1", state=JobState.PENDING)
    assert job.resource_override is False


@pytest.mark.asyncio
async def test_resource_override_persists_across_a_reload():
    job = Job(
        type="align_reads",
        owner="p1",
        state=JobState.PENDING,
        resource_override=True,
    )
    await job.insert()

    reloaded = await Job.get(job.id)
    assert reloaded is not None
    assert reloaded.resource_override is True


@pytest.mark.asyncio
async def test_enqueue_writes_override_into_the_redis_hash(redis, monkeypatch):
    from app.queue import keys, queue

    monkeypatch.setattr(queue, "get_redis", lambda: redis)

    job = await queue.enqueue(
        "align_reads", owner="p1", resource_override=True
    )
    assert job is not None

    value = await redis.hget(keys.job_key(str(job.id)), "override")
    assert value == "1"


@pytest.mark.asyncio
async def test_enqueue_writes_zero_when_not_overridden(redis, monkeypatch):
    from app.queue import keys, queue

    monkeypatch.setattr(queue, "get_redis", lambda: redis)

    job = await queue.enqueue("align_reads", owner="p1")
    assert job is not None

    value = await redis.hget(keys.job_key(str(job.id)), "override")
    # Written explicitly rather than omitted: claim.lua reads a fixed HMGET
    # position, and a missing field there is nil, not "0".
    assert value == "0"


@pytest.mark.asyncio
async def test_override_survives_a_hash_rebuild_by_reconcile(redis, monkeypatch):
    """The flag must come back on the hash, not merely on the document.

    Asserting the Mongo document still holds it would pass without this
    change -- Mongo is not what gets wiped by a Redis restart. The hash is
    what claim.lua reads, so the hash is what this asserts.
    """
    from app.queue import keys, queue

    monkeypatch.setattr(queue, "get_redis", lambda: redis)

    job = await queue.enqueue(
        "align_reads", owner="p1", resource_override=True
    )
    assert job is not None
    job_id = str(job.id)

    # Simulate the Redis-side loss a restart produces: the queue entry and
    # the hash both go, while Mongo keeps the job.
    await redis.delete(keys.job_key(job_id))
    await redis.zrem(keys.READY, job_id)

    await queue.reconcile()

    value = await redis.hget(keys.job_key(job_id), "override")
    assert value == "1"


async def _claim_with_budget(queue, mem_mb_budget: int, **kwargs):
    """Claim against a named memory budget, everything else generous."""
    return await queue.claim(
        "w1",
        allowed_classes=["user_background"],
        cpu_budget=64,
        mem_mb_budget=mem_mb_budget,
        io_heavy_budget=4,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_overridden_job_is_claimed_when_it_is_the_sole_occupant(
    redis, scripts, monkeypatch
):
    from app.models.job import JobResources
    from app.queue import queue

    monkeypatch.setattr(queue, "get_redis", lambda: redis)
    monkeypatch.setattr(queue, "get_script", lambda name: scripts[name])

    job = await queue.enqueue(
        "align_reads",
        owner="p1",
        resources=JobResources(mem_mb=8000),
        resource_override=True,
    )
    assert job is not None

    # Budget far below the job's declared need, and nothing else reserved.
    claimed = await _claim_with_budget(queue, 1000)
    assert claimed is not None
    assert claimed.job_id == str(job.id)


@pytest.mark.asyncio
async def test_overridden_job_is_refused_while_anything_else_holds_a_reservation(
    redis, scripts, monkeypatch
):
    """The direction that fails if `sole` is computed wrongly.

    The complementary "is claimed" assertion above would pass against a
    naive unconditional exemption too. This one would not.
    """
    from app.models.job import JobResources
    from app.queue import queue

    monkeypatch.setattr(queue, "get_redis", lambda: redis)
    monkeypatch.setattr(queue, "get_script", lambda name: scripts[name])

    # Something else is running and holding memory.
    await redis.set("bp:conc:mem_mb", 500)

    job = await queue.enqueue(
        "align_reads",
        owner="p1",
        resources=JobResources(mem_mb=8000),
        resource_override=True,
    )
    assert job is not None

    claimed = await _claim_with_budget(queue, 1000)
    assert claimed is None


@pytest.mark.asyncio
async def test_an_idle_worker_does_not_count_as_sole_occupancy(
    redis, scripts, monkeypatch
):
    """The `ignore_reservations` trap, tested on its own.

    With ignore_reservations set the counters are never read, so the
    reserved_* locals are zero because nothing was looked at. Treating that
    as "nothing is running" makes the override MORE permissive than an
    unconditional exemption, while reading in the source as if it were more
    conservative.
    """
    from app.models.job import JobResources
    from app.queue import queue

    monkeypatch.setattr(queue, "get_redis", lambda: redis)
    monkeypatch.setattr(queue, "get_script", lambda name: scripts[name])

    await redis.set("bp:conc:mem_mb", 500)

    job = await queue.enqueue(
        "align_reads",
        owner="p1",
        resources=JobResources(mem_mb=8000),
        resource_override=True,
    )
    assert job is not None

    claimed = await _claim_with_budget(queue, 1000, ignore_reservations=True)
    assert claimed is None


@pytest.mark.asyncio
async def test_a_non_overridden_job_is_still_refused_when_alone(
    redis, scripts, monkeypatch
):
    """The gate must still work for everyone else."""
    from app.models.job import JobResources
    from app.queue import queue

    monkeypatch.setattr(queue, "get_redis", lambda: redis)
    monkeypatch.setattr(queue, "get_script", lambda name: scripts[name])

    job = await queue.enqueue(
        "align_reads", owner="p1", resources=JobResources(mem_mb=8000)
    )
    assert job is not None

    claimed = await _claim_with_budget(queue, 1000)
    assert claimed is None


@pytest.mark.asyncio
async def test_override_does_not_relax_the_cpu_gate(redis, scripts, monkeypatch):
    """Scoped to memory. A CPU overcommit bands to WARN and never produces
    a card, so the override has no business touching it."""
    from app.models.job import JobResources
    from app.queue import queue

    monkeypatch.setattr(queue, "get_redis", lambda: redis)
    monkeypatch.setattr(queue, "get_script", lambda name: scripts[name])

    job = await queue.enqueue(
        "align_reads",
        owner="p1",
        resources=JobResources(cpu=32, mem_mb=100),
        resource_override=True,
    )
    assert job is not None

    claimed = await queue.claim(
        "w1",
        allowed_classes=["user_background"],
        cpu_budget=4,
        mem_mb_budget=64000,
        io_heavy_budget=4,
    )
    assert claimed is None
