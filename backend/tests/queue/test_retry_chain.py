"""A retried job must survive the whole retry -> promote -> claim chain.

The existing promote tests hand-write the `bp:job:{id}` hash before promoting,
so they never see what `retry_later` actually leaves behind. That is the gap
this file closes: `retry_later` released the lease with the terminal
(hash-deleting) mode and then re-added the bare id to the delayed set, so the
dispatch metadata claim.lua needs -- class, cpu, mem_mb, io, score -- was gone
by the time the job came due. promote_delayed moved a hashless id to ready and
claim.lua then dropped it as garbage, while Mongo still said `delayed`: the job
vanished from the queue on any transient error and only a worker restart's
reconcile could bring it back.

Every assertion here is on the state a *later* stage needs, not on the write
retry_later just made -- asserting "it is on the delayed zset" would pass with
the metadata already destroyed.
"""

import pytest
from beanie import init_beanie
from pymongo import AsyncMongoClient

from app.config import settings
from app.models import ALL_MODELS
from app.models.job import JobResources, JobState
from tests._mongo_isolation import direct_mongo_url, worker_db_name

NOW_MS = 1_700_000_000_000


@pytest.fixture(autouse=True)
async def _init_beanie_models(monkeypatch):
    """`retry_later` writes through Mongo, so a real database is required."""
    client = AsyncMongoClient(direct_mongo_url(settings.mongo_url), tz_aware=True)
    db = client[worker_db_name()]
    await init_beanie(database=db, document_models=ALL_MODELS)
    monkeypatch.setattr("app.db.client.get_db", lambda: db)
    yield
    await client.close()


async def _enqueue_and_claim(queue, **resources):
    """Get a job all the way to RUNNING, the state a retry starts from.

    `mark_running` is part of the setup, not incidental: claim bumps the epoch
    in Redis only, and it is mark_running that records the lease in Mongo. Skip
    it and every epoch-fenced write below is rejected as stale, so the code
    under test never runs.
    """
    job = await queue.enqueue(
        "align_reads",
        owner="p1",
        resources=JobResources(**resources) if resources else None,
    )
    assert job is not None
    claimed = await queue.claim(
        "w1",
        allowed_classes=["user_background"],
        cpu_budget=64,
        mem_mb_budget=64000,
        io_heavy_budget=8,
    )
    assert claimed is not None
    assert await queue.mark_running(claimed.job_id, "w1", claimed.epoch) is not None
    return job, claimed


@pytest.mark.asyncio
async def test_a_retried_job_keeps_the_metadata_claim_needs(redis, scripts, monkeypatch):
    """The dispatch hash must survive the retry release.

    Without it every later stage is broken at once, so this asserts the fields
    claim.lua actually reads rather than the hash's mere existence.
    """
    from app.queue import keys, queue

    monkeypatch.setattr(queue, "get_redis", lambda: redis)
    monkeypatch.setattr(queue, "get_script", lambda name: scripts[name])

    job, claimed = await _enqueue_and_claim(queue, cpu=4, mem_mb=2000)

    await queue.retry_later(str(job.id), claimed.epoch, attempts=1, error={"msg": "blip"})

    hash_after = await redis.hgetall(keys.job_key(str(job.id)))
    assert hash_after.get("class") == "user_background"
    assert hash_after.get("cpu") == "4"
    assert hash_after.get("mem_mb") == "2000"
    assert hash_after.get("score") is not None


@pytest.mark.asyncio
async def test_a_retried_job_is_claimable_again_once_due(redis, scripts, monkeypatch):
    """The whole chain: retry -> promote -> claim, with nothing hand-written.

    This is the assertion the bug actually broke. A job hit by a transient
    error must come back and run, not disappear while Mongo still lists it.
    """
    from app.queue import keys, queue

    monkeypatch.setattr(queue, "get_redis", lambda: redis)
    monkeypatch.setattr(queue, "get_script", lambda name: scripts[name])

    job, claimed = await _enqueue_and_claim(queue, cpu=2, mem_mb=512)
    job_id = str(job.id)

    await queue.retry_later(job_id, claimed.epoch, attempts=1, error={"msg": "blip"})

    # Its backoff elapses.
    due_at = await redis.zscore(keys.DELAYED, job_id)
    assert due_at is not None
    moved = await scripts["promote_delayed"](
        keys=[keys.DELAYED, keys.READY], args=[int(due_at) + 1, 100]
    )
    assert moved == [job_id], "the delayed job was not promoted"

    reclaimed = await queue.claim(
        "w2",
        allowed_classes=["user_background"],
        cpu_budget=64,
        mem_mb_budget=64000,
        io_heavy_budget=8,
    )
    assert reclaimed is not None, "the retried job was dropped instead of dispatched"
    assert reclaimed.job_id == job_id
    # The demand claim.lua reserved has to be the job's real demand, not a
    # default it fell back to with the hash missing.
    assert reclaimed.cpu == 2
    assert reclaimed.mem_mb == 512


@pytest.mark.asyncio
async def test_a_retried_job_keeps_its_priority_score(redis, scripts, monkeypatch):
    """Promotion must use the job's own score, not the wall clock.

    promote_delayed falls back to `now_ms` when it cannot read `score`, which
    silently reorders a retried job against the queue it rejoins -- a
    user_interactive retry landing behind bulk work.
    """
    from app.queue import keys, queue

    monkeypatch.setattr(queue, "get_redis", lambda: redis)
    monkeypatch.setattr(queue, "get_script", lambda name: scripts[name])

    job, claimed = await _enqueue_and_claim(queue)
    job_id = str(job.id)
    score_before = await redis.hget(keys.job_key(job_id), "score")

    await queue.retry_later(job_id, claimed.epoch, attempts=1, error={"msg": "blip"})
    due_at = await redis.zscore(keys.DELAYED, job_id)
    await scripts["promote_delayed"](
        keys=[keys.DELAYED, keys.READY], args=[int(due_at) + 1, 100]
    )

    assert await redis.zscore(keys.READY, job_id) == float(score_before)


@pytest.mark.asyncio
async def test_the_retry_releases_the_reservation(redis, scripts, monkeypatch):
    """Keeping the hash must not also keep the job's reservation.

    The lease is over the moment the retry is scheduled, so its cpu/mem must go
    back to the pool -- otherwise preserving the hash would trade a stranded job
    for a permanently shrunken admission budget.
    """
    from app.queue import keys, queue

    monkeypatch.setattr(queue, "get_redis", lambda: redis)
    monkeypatch.setattr(queue, "get_script", lambda name: scripts[name])

    job, claimed = await _enqueue_and_claim(queue, cpu=4, mem_mb=2000)
    assert int(await redis.get(keys.conc_key("mem_mb"))) == 2000

    await queue.retry_later(str(job.id), claimed.epoch, attempts=1, error={"msg": "blip"})

    assert int(await redis.get(keys.conc_key("mem_mb")) or 0) == 0
    assert int(await redis.get(keys.conc_key("cpu")) or 0) == 0
    # And the lease fields specifically are gone, so nothing reads a stale worker.
    hash_after = await redis.hgetall(keys.job_key(str(job.id)))
    assert "worker_id" not in hash_after
    assert "lease_expires" not in hash_after


@pytest.mark.asyncio
async def test_the_retry_carries_the_attempt_count_into_the_hash(
    redis, scripts, monkeypatch
):
    """A preserved hash must not still claim this is the job's first attempt.

    `reap_expired.lua` HINCRBYs the hash's `attempts` on a later lease expiry.
    Now that the hash survives a retry, leaving it at its enqueue-time value
    would restart that count and hand the job extra lives past max_attempts.
    """
    from app.queue import keys, queue

    monkeypatch.setattr(queue, "get_redis", lambda: redis)
    monkeypatch.setattr(queue, "get_script", lambda name: scripts[name])

    job, claimed = await _enqueue_and_claim(queue)

    await queue.retry_later(str(job.id), claimed.epoch, attempts=3, error={"msg": "blip"})

    assert await redis.hget(keys.job_key(str(job.id)), "attempts") == "3"


@pytest.mark.asyncio
async def test_a_terminal_completion_still_clears_the_hash(redis, scripts, monkeypatch):
    """The complementary direction: `complete` must still delete the hash.

    Preserving it on the retry path is only correct because the terminal path
    keeps discarding it; a fix that preserved both would leak a hash per job.
    """
    from app.queue import keys, queue

    monkeypatch.setattr(queue, "get_redis", lambda: redis)
    monkeypatch.setattr(queue, "get_script", lambda name: scripts[name])

    job, claimed = await _enqueue_and_claim(queue)
    job_id = str(job.id)

    await queue.complete(job_id, claimed.epoch, state=JobState.SUCCEEDED, result={})

    assert await redis.hgetall(keys.job_key(job_id)) == {}
    assert await redis.zscore(keys.RUNNING, job_id) is None
