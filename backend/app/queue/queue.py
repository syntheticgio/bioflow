"""Queue operations: enqueue, claim, complete, cancel, reconcile.

MongoDB is written first and is the record of truth; Redis is the dispatch
index. That ordering matters: a job that exists in Mongo but not Redis is
recoverable (the reconciler re-adds it), whereas the reverse would be a job
that dispatches with no durable record.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from beanie import PydanticObjectId
from pymongo.errors import DuplicateKeyError

from app.config import settings
from app.db.redis_client import get_redis, get_script
from app.logging import get_logger
from app.models import (
    ACTIVE_STATES,
    Job,
    JobClass,
    JobLease,
    JobResources,
    JobState,
    JobTiming,
)
from app.queue import keys
from app.queue.priority import (
    BASE_SCORES,
    PROMOTION_TARGET,
    compute_score,
    promotion_cutoff_score,
)

log = get_logger(__name__)

MAX_BACKOFF_SECONDS = 600
CLAIM_SCAN_LIMIT = 50


@dataclass
class ClaimedJob:
    job_id: str
    job_class: str
    cpu: int
    mem_mb: int
    io: str
    epoch: int


async def enqueue(
    job_type: str,
    *,
    payload: dict | None = None,
    job_class: JobClass = JobClass.USER_BACKGROUND,
    dedup_key: str | None = None,
    project_id: PydanticObjectId | None = None,
    object_id: PydanticObjectId | None = None,
    resources: JobResources | None = None,
    max_attempts: int | None = None,
    delay_seconds: float = 0,
) -> Job | None:
    """Create and dispatch a job. Returns None if deduplicated away.

    The Mongo insert is the deduplication guard: a unique partial index over
    non-terminal states means a concurrent duplicate raises DuplicateKeyError
    rather than producing two jobs.
    """
    now = datetime.now(UTC)
    resources = resources or JobResources()
    available_at = now + timedelta(seconds=delay_seconds) if delay_seconds > 0 else None

    job = Job(
        type=job_type,
        job_class=job_class,
        state=JobState.PENDING,
        payload=payload or {},
        dedup_key=dedup_key,
        project_id=project_id,
        object_id=object_id,
        resources=resources,
        max_attempts=max_attempts or settings.job_max_attempts,
        available_at=available_at,
        timing=JobTiming(enqueued_at=now),
    )

    try:
        await job.insert()
    except DuplicateKeyError:
        log.debug("job_deduplicated", type=job_type, dedup_key=dedup_key)
        return None

    await _push_to_redis(job, delay_seconds=delay_seconds)
    await publish_event("job.enqueued", {"job_id": str(job.id), "type": job_type})
    return job


async def _push_to_redis(job: Job, *, delay_seconds: float = 0) -> None:
    r = get_redis()
    job_id = str(job.id)
    score = compute_score(job.job_class, job.timing.enqueued_at)
    now_ms = int(datetime.now(UTC).timestamp() * 1000)

    pipe = r.pipeline()
    pipe.hset(
        keys.job_key(job_id),
        mapping={
            "type": job.type,
            "class": job.job_class.value,
            "cpu": job.resources.cpu,
            "mem_mb": job.resources.mem_mb,
            "io": job.resources.io.value,
            "attempts": job.attempts,
            "score": score,
            "epoch": job.lease.epoch if job.lease else 0,
        },
    )
    if delay_seconds > 0:
        pipe.zadd(keys.DELAYED, {job_id: now_ms + int(delay_seconds * 1000)})
        state = JobState.DELAYED
    else:
        pipe.zadd(keys.READY, {job_id: score})
        state = JobState.QUEUED
    await pipe.execute()

    await job.set({Job.state: state})


async def claim(
    worker_id: str,
    *,
    allowed_classes: list[str],
    cpu_free: int,
    mem_mb_free: int,
    io_heavy_free: int,
    lease_seconds: int | None = None,
) -> ClaimedJob | None:
    """Atomically claim the best dispatchable job, or None."""
    lease_ms = int((lease_seconds or settings.lease_ttl_seconds) * 1000)
    now_ms = int(datetime.now(UTC).timestamp() * 1000)

    result = await get_script("claim")(
        keys=[keys.READY, keys.RUNNING],
        args=[
            now_ms,
            lease_ms,
            worker_id,
            ",".join(allowed_classes),
            cpu_free,
            mem_mb_free,
            io_heavy_free,
            CLAIM_SCAN_LIMIT,
        ],
    )
    if not result:
        return None

    return ClaimedJob(
        job_id=result[0],
        job_class=result[1],
        cpu=int(result[2]),
        mem_mb=int(result[3]),
        io=result[4],
        epoch=int(result[5]),
    )


async def mark_running(job_id: str, worker_id: str, epoch: int) -> Job | None:
    """Record the lease in Mongo. Returns None if the job is gone or cancelled."""
    job = await Job.get(PydanticObjectId(job_id))
    if job is None:
        return None
    if job.cancel_requested or job.state is JobState.CANCELLED:
        return None

    now = datetime.now(UTC)
    expires = now + timedelta(seconds=settings.lease_ttl_seconds)
    await job.set(
        {
            Job.state: JobState.RUNNING,
            Job.lease: JobLease(
                worker_id=worker_id, expires_at=expires, heartbeat_at=now, epoch=epoch
            ),
            "timing.started_at": now,
            Job.updated_at: now,
        }
    )
    return await Job.get(PydanticObjectId(job_id))


async def heartbeat(job_ids: list[str], epochs: dict[str, int]) -> None:
    """Extend leases for in-flight jobs.

    The Mongo update is conditional on the epoch, so a worker that lost its
    lease while paused cannot resurrect it.
    """
    if not job_ids:
        return
    now = datetime.now(UTC)
    expires_ms = int((now.timestamp() + settings.lease_ttl_seconds) * 1000)

    r = get_redis()
    await r.zadd(keys.RUNNING, {jid: expires_ms for jid in job_ids})

    from app.db.client import get_db

    for jid in job_ids:
        await get_db().jobs.update_one(
            {"_id": PydanticObjectId(jid), "lease.epoch": epochs.get(jid, 0)},
            {
                "$set": {
                    "lease.heartbeat_at": now,
                    "lease.expires_at": now + timedelta(seconds=settings.lease_ttl_seconds),
                }
            },
        )


async def release(job_id: str, *, requeue: bool = False, score: float | None = None) -> bool:
    """Release a lease and its reserved resources. Idempotent."""
    result = await get_script("release")(
        keys=[keys.RUNNING, keys.READY],
        args=[job_id, "1" if requeue else "0", score or 0],
    )
    return bool(result)


async def complete(
    job_id: str,
    epoch: int,
    *,
    state: JobState,
    result: dict | None = None,
    error: dict | None = None,
) -> bool:
    """Write a terminal outcome, guarded by the fencing epoch.

    Returns False when the epoch no longer matches -- meaning this worker's
    lease was taken over and its result must be discarded.
    """
    from app.db.client import get_db

    now = datetime.now(UTC)
    job = await Job.get(PydanticObjectId(job_id))
    started = job.timing.started_at if job else None
    duration_ms = int((now - started).total_seconds() * 1000) if started else None

    update = {
        "state": state.value,
        "result": result,
        "error": error,
        "lease": None,
        "timing.finished_at": now,
        "timing.duration_ms": duration_ms,
        "updated_at": now,
        # Terminal jobs are pruned by the TTL index after 30 days.
        "expires_at": now + timedelta(days=30),
    }

    res = await get_db().jobs.update_one(
        {"_id": PydanticObjectId(job_id), "lease.epoch": epoch}, {"$set": update}
    )
    if res.matched_count == 0:
        log.warning("stale_epoch_write_rejected", job_id=job_id, epoch=epoch)
        return False

    await release(job_id, requeue=False)
    await publish_event(f"job.{state.value}", {"job_id": job_id})
    return True


async def retry_later(job_id: str, epoch: int, attempts: int, error: dict) -> None:
    """Schedule a retry with exponential backoff and jitter."""
    import random

    from app.db.client import get_db

    delay = min(MAX_BACKOFF_SECONDS, 2**attempts)
    delay *= 1 + random.uniform(-0.25, 0.25)  # jitter avoids retry convoys
    now = datetime.now(UTC)
    available_at = now + timedelta(seconds=delay)

    res = await get_db().jobs.update_one(
        {"_id": PydanticObjectId(job_id), "lease.epoch": epoch},
        {
            "$set": {
                "state": JobState.DELAYED.value,
                "error": error,
                "lease": None,
                "available_at": available_at,
                "updated_at": now,
            }
        },
    )
    if res.matched_count == 0:
        log.warning("stale_epoch_retry_rejected", job_id=job_id, epoch=epoch)
        return

    r = get_redis()
    await release(job_id, requeue=False)
    await r.zadd(keys.DELAYED, {job_id: int(available_at.timestamp() * 1000)})
    log.info("job_retry_scheduled", job_id=job_id, delay_s=round(delay, 1))


async def request_cancel(job_id: str) -> str:
    """Request cancellation. Returns the resulting disposition.

    Queued jobs are cancelled synchronously; running jobs are signalled and
    stop cooperatively, so the API reports 'cancelling' rather than pretending
    it happened instantly.
    """
    job = await Job.get(PydanticObjectId(job_id))
    if job is None:
        return "not_found"
    if job.state in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED, JobState.DEAD):
        return "already_terminal"

    now = datetime.now(UTC)
    r = get_redis()
    await r.sadd(keys.CANCEL, job_id)
    await job.set({Job.cancel_requested: True, Job.updated_at: now})

    if job.state in (JobState.QUEUED, JobState.DELAYED, JobState.PENDING):
        pipe = r.pipeline()
        pipe.zrem(keys.READY, job_id)
        pipe.zrem(keys.DELAYED, job_id)
        pipe.delete(keys.job_key(job_id))
        pipe.srem(keys.CANCEL, job_id)
        await pipe.execute()
        await job.set(
            {
                Job.state: JobState.CANCELLED,
                "timing.finished_at": now,
                Job.expires_at: now + timedelta(days=30),
            }
        )
        await publish_event("job.cancelled", {"job_id": job_id})
        return "cancelled"

    await publish_event("job.cancel_requested", {"job_id": job_id})
    return "cancelling"


async def is_cancel_requested(job_id: str) -> bool:
    return bool(await get_redis().sismember(keys.CANCEL, job_id))


async def promote_delayed(max_batch: int = 100) -> list[str]:
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    moved = await get_script("promote_delayed")(
        keys=[keys.DELAYED, keys.READY], args=[now_ms, max_batch]
    )
    if moved:
        from app.db.client import get_db

        await get_db().jobs.update_many(
            {"_id": {"$in": [PydanticObjectId(m) for m in moved]}},
            {"$set": {"state": JobState.QUEUED.value}},
        )
    return moved or []


async def promote_aged(max_batch: int = 200) -> int:
    """Run one anti-starvation promotion sweep across all promotable classes."""
    total = 0
    now = datetime.now(UTC)
    for job_class, target in PROMOTION_TARGET.items():
        cutoff = promotion_cutoff_score(job_class, now)
        promoted = await get_script("promote_aged")(
            keys=[keys.READY],
            args=[cutoff, BASE_SCORES[job_class], BASE_SCORES[target], max_batch],
        )
        total += int(promoted or 0)
    if total:
        log.info("jobs_promoted", count=total)
    return total


async def reap_expired(max_batch: int = 100) -> list[tuple[str, int]]:
    """Requeue jobs whose leases expired, failing those out of attempts."""
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    flat = await get_script("reap_expired")(
        keys=[keys.RUNNING, keys.READY], args=[now_ms, max_batch]
    )
    if not flat:
        return []

    pairs = [(flat[i], int(flat[i + 1])) for i in range(0, len(flat), 2)]
    from app.db.client import get_db

    for job_id, attempts in pairs:
        job = await Job.get(PydanticObjectId(job_id))
        if job is None:
            continue
        if attempts >= job.max_attempts:
            await get_db().jobs.update_one(
                {"_id": PydanticObjectId(job_id)},
                {
                    "$set": {
                        "state": JobState.DEAD.value,
                        "attempts": attempts,
                        "lease": None,
                        "error": {
                            "code": "lease_expired",
                            "message": "Exceeded max attempts after repeated lease expiry",
                            "retryable": False,
                        },
                        "expires_at": datetime.now(UTC) + timedelta(days=30),
                    }
                },
            )
            await get_redis().zrem(keys.READY, job_id)
            log.error("job_dead_after_lease_expiry", job_id=job_id, attempts=attempts)
        else:
            await get_db().jobs.update_one(
                {"_id": PydanticObjectId(job_id)},
                {
                    "$set": {
                        "state": JobState.QUEUED.value,
                        "attempts": attempts,
                        "lease": None,
                    }
                },
            )
            log.warning("job_requeued_lease_expired", job_id=job_id, attempts=attempts)

    return pairs


async def rescue_orphans(older_than_seconds: float = 60.0) -> int:
    """Re-queue jobs that exist in Mongo but never reached Redis.

    `enqueue` writes the durable record first, then pushes to Redis. A crash
    between those two steps leaves a job stranded in PENDING that nothing will
    ever dispatch. `reconcile` covers this at startup, but a process that dies
    mid-enqueue while the workers keep running would otherwise strand the job
    until the next restart -- so the leader also sweeps periodically.

    The age threshold avoids racing a healthy enqueue that is simply between
    its two steps right now.
    """
    from datetime import timedelta

    cutoff = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
    rescued = 0
    r = get_redis()

    async for job in Job.find(
        {"state": JobState.PENDING.value, "created_at": {"$lt": cutoff}}
    ):
        job_id = str(job.id)
        if await r.zscore(keys.READY, job_id) is not None:
            continue
        if await r.zscore(keys.DELAYED, job_id) is not None:
            continue
        await _push_to_redis(job)
        rescued += 1
        log.warning("orphaned_job_rescued", job_id=job_id, type=job.type)

    return rescued


async def reconcile() -> int:
    """Rebuild Redis dispatch state from MongoDB.

    Run at startup. Without it, an AOF loss or a flushed Redis would silently
    strand every queued job -- they would still exist in Mongo, but nothing
    would ever dispatch them.
    """
    r = get_redis()
    restored = 0
    now_ms = int(datetime.now(UTC).timestamp() * 1000)

    async for job in Job.find({"state": {"$in": [s.value for s in ACTIVE_STATES]}}):
        job_id = str(job.id)
        if await r.zscore(keys.READY, job_id) is not None:
            continue
        if await r.zscore(keys.DELAYED, job_id) is not None:
            continue
        if job.state is JobState.RUNNING and await r.zscore(keys.RUNNING, job_id) is not None:
            continue

        score = compute_score(job.job_class, job.timing.enqueued_at)
        pipe = r.pipeline()
        pipe.hset(
            keys.job_key(job_id),
            mapping={
                "type": job.type,
                "class": job.job_class.value,
                "cpu": job.resources.cpu,
                "mem_mb": job.resources.mem_mb,
                "io": job.resources.io.value,
                "attempts": job.attempts,
                "score": score,
                "epoch": job.lease.epoch if job.lease else 0,
            },
        )
        if job.available_at and job.available_at > datetime.now(UTC):
            pipe.zadd(keys.DELAYED, {job_id: int(job.available_at.timestamp() * 1000)})
        else:
            pipe.zadd(keys.READY, {job_id: score})
        await pipe.execute()

        # A job recorded as RUNNING with no live lease was orphaned by a crash.
        if job.state is JobState.RUNNING:
            await job.set({Job.state: JobState.QUEUED, Job.lease: None})
        restored += 1

    if restored:
        log.info("queue_reconciled", restored=restored, now_ms=now_ms)
    return restored


async def publish_event(event_type: str, data: dict) -> None:
    """Fan out to SSE subscribers via Redis pub/sub.

    Events are advisory: the UI refetches on receipt rather than treating the
    payload as authoritative, so a dropped message costs a delay, not accuracy.
    """
    try:
        await get_redis().publish(
            keys.EVENTS, json.dumps({"type": event_type, "data": data})
        )
    except Exception as e:  # noqa: BLE001 - never fail a job over telemetry
        log.debug("event_publish_failed", error=str(e))
