"""Queue operations: enqueue, claim, complete, cancel, reconcile.

MongoDB is written first and is the record of truth; Redis is the dispatch
index. That ordering matters: a job that exists in Mongo but not Redis is
recoverable (the reconciler re-adds it), whereas the reverse would be a job
that dispatches with no durable record.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psutil
from beanie import PydanticObjectId
from pymongo.errors import DuplicateKeyError

from app.api.deps import target_node_ctx
from app.config import settings
from app.db.redis_client import get_redis, get_script
from app.errors import ValidationError
from app.logging import get_logger
from app.models import (
    ACTIVE_STATES,
    TERMINAL_STATES,
    AttemptProgress,
    IoClass,
    Job,
    JobClass,
    JobLease,
    JobProgress,
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
    owner: str,
    payload: dict | None = None,
    job_class: JobClass = JobClass.USER_BACKGROUND,
    dedup_key: str | None = None,
    project_id: PydanticObjectId | None = None,
    object_id: PydanticObjectId | None = None,
    resources: JobResources | None = None,
    max_attempts: int | None = None,
    delay_seconds: float = 0,
    depends_on: list[PydanticObjectId] | None = None,
    tolerate_failure_of: list[PydanticObjectId] | None = None,
    parent_job_id: PydanticObjectId | None = None,
    resource_override: bool = False,
    target_node: str | None = None,
) -> Job | None:
    """Create and dispatch a job. Returns None if deduplicated away.

    The Mongo insert is the deduplication guard: a unique partial index over
    non-terminal states means a concurrent duplicate raises DuplicateKeyError
    rather than producing two jobs.

    `owner` is folded into the stored dedup key rather than being left to each
    caller to remember. Deduplication reports itself as a `None` return, not an
    error, so a key that collides across profiles loses a job in total silence:
    two of the keys this codebase builds -- `build_index:{digest}:{aligner}`
    and `index_bam:{sha256}` -- are derived from blob content alone, and blobs
    are global and shared by design, so the second profile to align against a
    shared reference genome would have got `None` back and no index. Folding it
    in here means a call site someone adds later, that builds a purely
    content-scoped key and never reads this warning, is safe by construction --
    which is the whole reason this lives in `enqueue` rather than in the two
    call sites that happen to need it today.

    The honest cost: identical work is no longer shared between profiles. A
    reference genome that two profiles both align against is now indexed twice,
    once per profile, where before it was indexed once. That is the correct
    trade -- a profile's job must not vanish because another profile happened
    to ask for the same thing first -- but it does mean a shared `build_index`
    is no longer actually shared.

    Worth knowing the size of that cost before it surprises someone: an aligner
    index of a large genome is gigabytes, not megabytes, so each additional
    profile aligning against the same reference pays that again in both disk
    and build time. Acceptable for the handful of profiles this tool is built
    for; it would not be at a scale this tool does not target. Note the *blobs*
    are still shared -- only the derived index is duplicated, because it is a
    sidecar of a per-profile reference object rather than content-addressed
    work.

    `depends_on` holds the job back until every listed job has *succeeded*.
    Such a job is never pushed to Redis; `_release_dependents` puts it there
    when its last dependency finishes. If any dependency fails, the dependent
    fails too, with that dependency named as the reason -- an alignment whose
    index build died must not sit queued forever waiting for a file that is
    never coming.

    Both halves of that are enforced by the `_handle_dependencies` gate below
    rather than by the branches inside it. Until #442 they were not enforced at
    all: the branches set the right state and returned, and the push happened
    anyway, so a blocked job went into the ready set to be claimed and run
    before its input existed.
    """
    now = datetime.now(UTC)
    # Auto-detect target_node from the HTTP request context (set by middleware
    # in main.py). An explicit parameter always wins; None means "check the URL."
    _node = target_node if target_node is not None else target_node_ctx.get()
    resources = resources or JobResources()
    available_at = now + timedelta(seconds=delay_seconds) if delay_seconds > 0 else None

    # Fail immediately if the job asks for more memory than this machine has.
    # Without this check the job sits in the ready set forever — claim.lua can
    # never admit it, but nothing tells the user why. This is a permanent
    # error, not a retry: raising the machine's RAM is the only fix.
    if resources.mem_mb:
        machine_mb = int(psutil.virtual_memory().total / (1024 * 1024))
        if resources.mem_mb > machine_mb:
            raise ValidationError(
                f"Job requires {resources.mem_mb} MB of RAM, "
                f"but this machine only has {machine_mb} MB. "
                "Raise the Docker Desktop memory allocation or reduce the job's memory request."
            )

    depends_on = list(depends_on or [])
    tolerate_failure_of = list(tolerate_failure_of or [])

    # Left None when the caller passed None: the unique index's $type clause
    # exempts missing keys, and turning "no deduplication" into the bare owner
    # string would collide every opted-out job in a profile with every other.
    stored_dedup_key = f"{owner}:{dedup_key}" if dedup_key is not None else None

    job = await _handle_dedup(job_type, owner, job_class, payload, stored_dedup_key,
                              project_id, object_id, resources, max_attempts,
                              available_at, depends_on, tolerate_failure_of,
                              parent_job_id, resource_override, now)
    if job is None:
        return None

    # Gated, not sequential: `_handle_dependencies` may have already failed the
    # job or parked it as BLOCKED, and `_push_to_redis` ends in an
    # unconditional state write that would overwrite either one -- putting a
    # job in the ready set to be claimed and run without the input it is
    # waiting for.
    if depends_on and not await _handle_dependencies(
        job, depends_on, tolerate_failure_of, job_type, owner
    ):
        return job

    await _push_to_redis(job, delay_seconds=delay_seconds, target_node=_node)
    await publish_event(
        "job.enqueued", {"job_id": str(job.id), "type": job_type}, owner=owner
    )
    return job


async def _handle_dedup(
    job_type: str,
    owner: str,
    job_class: JobClass,
    payload: dict | None,
    stored_dedup_key: str | None,
    project_id: PydanticObjectId | None,
    object_id: PydanticObjectId | None,
    resources: JobResources,
    max_attempts: int | None,
    available_at: datetime | None,
    depends_on: list[PydanticObjectId],
    tolerate_failure_of: list[PydanticObjectId],
    parent_job_id: PydanticObjectId | None,
    resource_override: bool,
    now: datetime,
) -> Job | None:
    """Create and insert a job, returning None if a duplicate exists.

    Deduplication is enforced by a unique partial index on non-terminal states:
    a concurrent duplicate raises DuplicateKeyError rather than producing two
    jobs. The caller's dedup_key is already folded with owner by enqueue().
    """
    job = Job(
        type=job_type,
        owner=owner,
        job_class=job_class,
        state=JobState.PENDING,
        payload=payload or {},
        dedup_key=stored_dedup_key,
        project_id=project_id,
        object_id=object_id,
        resources=resources,
        max_attempts=max_attempts or settings.job_max_attempts,
        available_at=available_at,
        depends_on=depends_on,
        tolerate_failure_of=tolerate_failure_of,
        parent_job_id=parent_job_id,
        resource_override=resource_override,
        timing=JobTiming(enqueued_at=now),
    )
    try:
        await job.insert()
    except DuplicateKeyError:
        log.debug("job_deduplicated", type=job_type, dedup_key=stored_dedup_key)
        return None
    return job


async def _handle_dependencies(
    job: Job,
    depends_on: list[PydanticObjectId],
    tolerate_failure_of: list[PydanticObjectId],
    job_type: str,
    owner: str,
) -> bool:
    """Resolve dependency chain for a job that has dependencies.

    Returns whether the job is dispatchable -- False when it has been failed
    outright or parked as BLOCKED, True when every dependency is satisfied and
    the caller should go on to push it. The three outcomes were previously
    indistinguishable to `enqueue`, which pushed the job in all of them; the
    unconditional state write at the end of `_push_to_redis` then overwrote
    whichever decision had just been made.

    Re-reads dependencies *after* inserting, never before. A dependency that
    finished during the insert would otherwise be missed by both sides:
    `_release_dependents` could not see a job that did not exist yet, and a
    pre-insert check would not have seen it finish. Checking afterwards means
    the job is already visible to any concurrent completion, so at worst both
    paths try to release it -- which the conditional state update in
    `_release_dependents` makes safe.
    """
    outstanding = await _unfinished_dependencies(depends_on, tolerate_failure_of)
    failed = await _failed_dependencies(depends_on, tolerate_failure_of)
    if failed:
        await _fail_blocked_job(job, failed)
        return False
    if outstanding:
        await job.set({Job.state: JobState.BLOCKED})
        log.info(
            "job_blocked",
            job_id=str(job.id),
            type=job_type,
            waiting_on=[str(d) for d in outstanding],
        )
        await publish_event(
            "job.enqueued", {"job_id": str(job.id), "type": job_type}, owner=owner
        )
        return False

    # All dependencies satisfied; the caller pushes it to Redis.
    return True


def classify_dependencies(
    jobs: list[Job],
    *,
    tolerate_failure_of: set[PydanticObjectId] | None = None,
) -> tuple[list[Job], list[Job]]:
    """Split dependency jobs into (unfinished, failed).

    Pure, so the release decision can be tested without a database -- the
    decision itself is the part worth getting right, and it is easy to get
    subtly wrong in a way only a rare interleaving reveals.

    A dependency id with no job behind it is neither unfinished nor failed: the
    record was pruned by the 30-day TTL, or never existed. Treating a missing
    job as blocking would strand otherwise-ready work forever, and treating it
    as failed would kill work whose input very likely did get produced.

    `tolerate_failure_of` names dependencies whose failure must not cascade --
    workflow nodes marked `continue_on_failure`. It is a set of ids rather than
    a boolean because tolerance is per-edge: a node may depend on both an
    optional QC step and a mandatory alignment, and only the first is
    survivable. Note that a tolerated dependency still *blocks* while active;
    tolerating a failure is not the same as not waiting for the work.
    """
    tolerated = tolerate_failure_of or set()
    unfinished = [j for j in jobs if j.state in ACTIVE_STATES]
    failed = [
        j
        for j in jobs
        if j.state in TERMINAL_STATES
        and j.state is not JobState.SUCCEEDED
        and j.id not in tolerated
    ]
    return unfinished, failed


async def _dependency_jobs(depends_on: list[PydanticObjectId]) -> list[Job]:
    return await Job.find({"_id": {"$in": depends_on}}).to_list()


async def _unfinished_dependencies(
    depends_on: list[PydanticObjectId],
    tolerate_failure_of: list[PydanticObjectId] | None = None,
) -> list[PydanticObjectId]:
    unfinished, _ = classify_dependencies(
        await _dependency_jobs(depends_on),
        tolerate_failure_of=set(tolerate_failure_of or ()),
    )
    return [j.id for j in unfinished]


async def _failed_dependencies(
    depends_on: list[PydanticObjectId],
    tolerate_failure_of: list[PydanticObjectId] | None = None,
) -> list[Job]:
    """Dependencies that ended badly *and* were not tolerated.

    Both callers pass the dependent's own `tolerate_failure_of`, which is why
    this takes it rather than reading it off a job: `enqueue` asks about a job
    it has only just inserted, and `_release_dependents` asks on behalf of each
    of several dependents in turn, each with its own tolerated set.
    """
    _, failed = classify_dependencies(
        await _dependency_jobs(depends_on),
        tolerate_failure_of=set(tolerate_failure_of or ()),
    )
    return failed


async def _advance_workflow(job_id: str, *, succeeded: bool) -> None:
    """Tell any workflow waiting on this job that it has finished.

    `complete()` is the usual path to a terminal state and calls the hook
    itself, but it is not the only one: a job can be failed because a
    dependency failed, cancelled while still queued or blocked, or declared
    dead after repeated lease expiry. Each of those writes a terminal state
    without ever reaching `complete()`, and each used to leave the workflow
    node it belonged to sitting on RUNNING forever -- nothing reclaims it,
    because a node holds no lease to expire. A real OOM-killed `build_index`
    stranded its whole workflow that way.

    The hook itself never raises (it swallows and logs), so this is safe to
    call from paths whose real job is something else.
    """
    from app.services import workflow_hook

    await workflow_hook.on_job_finished(PydanticObjectId(job_id), succeeded=succeeded)


async def _clear_cancel_flag(job_id: str) -> None:
    """Drop a job id from the cancel set once nothing can still read it.

    Every worker polls this set once a second, so a stale entry is a cost paid
    forever by every worker. Failures are swallowed: this is hygiene, and a
    Redis blip must not turn into a failed job.
    """
    try:
        await get_redis().srem(keys.CANCEL, job_id)
    except Exception as e:  # noqa: BLE001
        log.debug("cancel_flag_clear_failed", job_id=job_id, error=str(e))


async def _fail_blocked_job(job: Job, failed: list[Job]) -> None:
    """Fail a dependent because something it needed did not succeed.

    The dependency's own error is quoted rather than summarised, because "the
    index build failed" without saying *how* sends the user hunting through
    job history for the real message.
    """
    from app.db.client import get_db

    culprit = failed[0]
    detail = culprit.error.message if culprit.error else f"it ended as {culprit.state.value}"
    message = f"Dependency {culprit.type} ({culprit.id}) did not succeed: {detail}"

    now = datetime.now(UTC)
    await get_db().jobs.update_one(
        {"_id": job.id, "state": {"$in": [JobState.BLOCKED.value, JobState.PENDING.value]}},
        {
            "$set": {
                "state": JobState.FAILED.value,
                "error": {
                    "code": "dependency_failed",
                    "message": message,
                    "traceback_tail": "",
                    # Retrying cannot help: the missing input is still missing.
                    "retryable": False,
                },
                "lease": None,
                "timing.finished_at": now,
                "updated_at": now,
                "expires_at": now + timedelta(days=30),
            }
        },
    )
    log.warning(
        "job_dependency_failed",
        job_id=str(job.id),
        type=job.type,
        dependency_id=str(culprit.id),
        dependency_type=culprit.type,
    )
    await publish_event("job.failed", {"job_id": str(job.id)}, owner=job.owner)
    await _clear_cancel_flag(str(job.id))
    await _advance_workflow(str(job.id), succeeded=False)

    # Cascade: anything waiting on *this* job now cannot run either.
    await _release_dependents(str(job.id), succeeded=False)


async def _release_dependents(job_id: str, *, succeeded: bool) -> None:
    """Unblock (or fail) the jobs waiting on a job that just finished.

    Called on every terminal outcome. On success a dependent is dispatched only
    once *all* its dependencies are done, so a job waiting on two index builds
    is released by the second one to finish, not the first.
    """
    dependents = await Job.find(
        {"depends_on": PydanticObjectId(job_id), "state": JobState.BLOCKED.value}
    ).to_list()
    if not dependents:
        return

    for dep in dependents:
        if not succeeded:
            failed = await _failed_dependencies(dep.depends_on, dep.tolerate_failure_of)
            if failed:
                await _fail_blocked_job(dep, failed)
                continue
            # Every failure this dependent saw was tolerated, so it is not
            # doomed -- but it may still be waiting on a sibling. Fall through
            # to the same readiness check the success path uses rather than
            # leaving it BLOCKED forever: this failure was its last chance at a
            # release, since nothing else is coming to wake it.

        if await _unfinished_dependencies(dep.depends_on, dep.tolerate_failure_of):
            continue  # still waiting on a sibling dependency

        # Conditional on the job still being BLOCKED, so two dependencies
        # finishing at once cannot both dispatch it.
        from app.db.client import get_db

        claimed = await get_db().jobs.update_one(
            {"_id": dep.id, "state": JobState.BLOCKED.value},
            {"$set": {"state": JobState.PENDING.value, "updated_at": datetime.now(UTC)}},
        )
        if claimed.modified_count == 0:
            continue

        fresh = await Job.get(dep.id)
        if fresh is None:
            continue
        await _push_to_redis(fresh)
        log.info("job_unblocked", job_id=str(dep.id), type=dep.type, after=job_id)


async def _push_to_redis(
    job: Job, *, delay_seconds: float = 0, target_node: str | None = None
) -> None:
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
            # Always written, never omitted: claim.lua reads this at a fixed
            # HMGET position, where an absent field is nil rather than "0".
            "override": "1" if job.resource_override else "0",
            # Node the job is targeted at, or empty for the global pool.
            # Written so release/reap know which per-node counters to decrement.
            "node": target_node or "",
        },
    )
    ready_key = keys.ready_key(target_node)
    if delay_seconds > 0:
        pipe.zadd(keys.DELAYED, {job_id: now_ms + int(delay_seconds * 1000)})
        state = JobState.DELAYED
    else:
        pipe.zadd(ready_key, {job_id: score})
        state = JobState.QUEUED
    await pipe.execute()

    await job.set({Job.state: state})


async def claim(
    worker_id: str,
    *,
    allowed_classes: list[str],
    cpu_budget: int,
    mem_mb_budget: int,
    io_heavy_budget: int,
    ignore_reservations: bool = False,
    lease_seconds: int | None = None,
    node_id: str = "",
    ready_key: str | None = None,
) -> ClaimedJob | None:
    """Atomically claim the best dispatchable job, or None.

    Budgets are the ceiling before reservations, not headroom after them:
    claim.lua reads the live `bp:conc:*` counters itself, inside the same
    atomic execution that reserves and grants the lease, rather than trusting
    a free-capacity value the caller computed moments earlier from a separate
    round trip. `ignore_reservations` carries the caller's in-flight
    self-healing clamp (see worker.compute_free_resources) through to the
    script, since that decision depends on per-worker local state Lua cannot
    see.
    """
    lease_ms = int((lease_seconds or settings.lease_ttl_seconds) * 1000)
    now_ms = int(datetime.now(UTC).timestamp() * 1000)

    result = await get_script("claim")(
        keys=[ready_key or keys.READY, keys.RUNNING],
        args=[
            now_ms,
            lease_ms,
            worker_id,
            ",".join(allowed_classes),
            cpu_budget,
            mem_mb_budget,
            io_heavy_budget,
            CLAIM_SCAN_LIMIT,
            "1" if ignore_reservations else "0",
            node_id,
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
    """Record the lease in Mongo. Returns None if the job is gone or cancelled.

    Also where a later attempt's stale progress gets reset. A job that died
    mid-run and was requeued (lease expiry, retry backoff) still carries
    whatever `progress` it last reported -- 80% on a run that is restarting
    from zero, if nothing intervenes. `attempts > 0` is the signal: `retry_later`
    and `reap_expired` both increment `attempts` before scheduling the next
    try, so by the time this runs for that try, `job.attempts` is already
    what the *previous* attempt reached. `mark_running` is the once-per-attempt
    write that starts a job running again, so it is the one place this needs
    to happen regardless of which requeue path led here.

    A terminal failure is untouched by this: nothing here runs on that path,
    so a failed job keeps showing what it was doing when it died -- already
    correct, and the more useful of the two behaviours for that case.
    """
    job = await Job.get(PydanticObjectId(job_id))
    if job is None:
        return None
    if job.cancel_requested or job.state is JobState.CANCELLED:
        return None

    now = datetime.now(UTC)
    expires = now + timedelta(seconds=settings.lease_ttl_seconds)
    update = {
        Job.state: JobState.RUNNING,
        Job.lease: JobLease(
            worker_id=worker_id, expires_at=expires, heartbeat_at=now, epoch=epoch
        ),
        "timing.started_at": now,
        Job.updated_at: now,
    }

    had_progress = (
        job.progress.pct is not None or job.progress.phase != "" or job.progress.message != ""
    )
    if job.attempts > 0 and had_progress:
        update[Job.last_attempt_progress] = AttemptProgress(
            attempt=job.attempts,
            pct=job.progress.pct,
            phase=job.progress.phase,
            message=job.progress.message,
            peak_rss_bytes=job.progress.peak_rss_bytes,
        )
        update[Job.progress] = JobProgress()

    await job.set(update)
    return await Job.get(PydanticObjectId(job_id))


async def _heartbeat_mongo(
    job_ids: list[str], epochs: dict[str, int], ttls: dict[str, int], now: datetime
) -> None:
    """Write the renewed lease to Mongo, one conditional update per job.

    Split out from `heartbeat` so the Redis half -- the part the reaper actually
    compares against -- is testable without a database.
    """
    from app.db.client import get_db

    for jid in job_ids:
        ttl = ttls.get(jid, settings.lease_ttl_seconds)
        await get_db().jobs.update_one(
            {"_id": PydanticObjectId(jid), "lease.epoch": epochs.get(jid, 0)},
            {
                "$set": {
                    "lease.heartbeat_at": now,
                    "lease.expires_at": now + timedelta(seconds=ttl),
                }
            },
        )


async def heartbeat(
    job_ids: list[str],
    epochs: dict[str, int],
    ttls: dict[str, int] | None = None,
) -> None:
    """Extend leases for in-flight jobs.

    `ttls` carries per-job lease lengths for handlers that called
    `ctx.extend_lease` -- anything absent renews to the global default. The
    distinction only bites when heartbeating *stops*: a paused VM leaves the
    recorded expiry as the sole thing standing between a live job and the
    reaper, and a job that said it needed an hour must not be holding a 30s
    lease at that moment.

    The Mongo update is conditional on the epoch, so a worker that lost its
    lease while paused cannot resurrect it.
    """
    if not job_ids:
        return
    ttls = ttls or {}
    now = datetime.now(UTC)

    r = get_redis()
    await r.zadd(
        keys.RUNNING,
        {
            jid: int(
                (now.timestamp() + ttls.get(jid, settings.lease_ttl_seconds)) * 1000
            )
            for jid in job_ids
        },
    )

    await _heartbeat_mongo(job_ids, epochs, ttls, now)


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

    # The job document is looked up above for its start time and may be None --
    # the update_one above works off `job_id` alone, so a completion can land
    # for a document that has since been deleted. Do *not* fall back to "local"
    # for the event's owner in that case: "local" belongs to the profile that
    # adopted the pre-profiles library, and attributing a stranger's job to it
    # is exactly the leak per-owner channels exist to prevent. It goes to the
    # system channel instead, and gets logged, because a job completing with no
    # document behind it is a bug worth seeing rather than a routine case.
    if job is None:
        log.warning("completed_job_document_missing", job_id=job_id, state=state.value)
    await publish_event(
        f"job.{state.value}",
        {"job_id": job_id},
        owner=job.owner if job else keys.SYSTEM_OWNER,
    )

    # After the terminal write lands, so a dependent that dispatches
    # immediately cannot observe its dependency as still running.
    await _release_dependents(job_id, succeeded=state is JobState.SUCCEEDED)

    # Advance any workflow waiting on this job. A no-op (one indexed query) for
    # the overwhelming majority of jobs, which belong to no workflow, and it
    # never raises -- workflow bookkeeping must not turn a successful job into
    # a failed one.
    await _advance_workflow(job_id, succeeded=state is JobState.SUCCEEDED)
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

    # BLOCKED included: such a job has no Redis presence to clear, but it is
    # cancellable exactly like a queued one, and its dependents must be told.
    if job.state in (JobState.QUEUED, JobState.DELAYED, JobState.PENDING, JobState.BLOCKED):
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
        await publish_event("job.cancelled", {"job_id": job_id}, owner=job.owner)
        await _advance_workflow(job_id, succeeded=False)
        # Cancelling an index build must not leave the alignment behind it
        # queued forever waiting for a file nobody is going to write.
        await _release_dependents(job_id, succeeded=False)
        return "cancelled"

    await publish_event("job.cancel_requested", {"job_id": job_id}, owner=job.owner)
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
            # This job is terminal and will never run again, so nothing will
            # ever observe its cancel flag. Left behind, the id is polled by
            # every worker once a second forever.
            await _clear_cancel_flag(job_id)
            log.error("job_dead_after_lease_expiry", job_id=job_id, attempts=attempts)
            # A job that died here never went through `complete`, so this is
            # the only place its dependents -- and any workflow node it serves
            # -- learn they will never run.
            await _advance_workflow(job_id, succeeded=False)
            await _release_dependents(job_id, succeeded=False)
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


async def _rebuild_reservation_counters(r) -> int:
    """Reset every `bp:conc:*` counter to the true sum over the RUNNING set.

    The counters are the admission gate claim.lua reads, and each is meant to
    equal the summed demand of the jobs currently leased (cpu and mem_mb) or the
    count of heavy-IO leases (io_heavy), scoped per node. claim.lua INCRBYs on
    grant and release.lua DECRBYs on every terminal outcome -- but a handful of
    paths break that pairing: a cancel that races a claim deletes the dispatch
    hash the release would have read its amounts from, a non-idempotent retry
    increments twice, and a hard crash between INCRBY and release leaves the
    increment with no counterpart. Nothing else ever zeroes the counters, so any
    such leak is permanent until Redis is flushed -- a phantom reservation that
    shrinks headroom forever, and once it pushes mem_mb above the budget it
    silently refuses every future job.

    Startup is the one moment this can be made authoritative: the RUNNING zset
    plus each job's dispatch hash is the exact state claim.lua reserved from, so
    summing it and writing the totals back makes the whole leak class
    self-healing. Reservations for scopes with nothing running are cleared; a
    deleted key reads as zero to claim.lua (`tonumber(nil) or 0`), same as "0".

    Returns the number of live RUNNING leases counted, for the reconcile log.
    """
    # {node_id ("" for global): [cpu, mem_mb, io_heavy]}
    totals: dict[str, list[int]] = {}
    job_ids = await r.zrange(keys.RUNNING, 0, -1)
    for job_id in job_ids:
        cpu, mem_mb, io, node = await r.hmget(
            keys.job_key(job_id), "cpu", "mem_mb", "io", "node"
        )
        if cpu is None and mem_mb is None:
            # A RUNNING member whose dispatch hash is gone reserved nothing this
            # rebuild can attribute; the per-job loop below handles the orphan.
            continue
        bucket = totals.setdefault(node or "", [0, 0, 0])
        bucket[0] += int(cpu or 0)
        bucket[1] += int(mem_mb or 0)
        if io == IoClass.HEAVY.value:
            bucket[2] += 1

    # Clear every counter that currently exists, then write the true totals.
    # Clearing first is what drains a leak on a scope that now has nothing
    # running -- it would never appear in `totals` to be overwritten.
    existing = set()
    for resource in ("cpu", "mem_mb", "io_heavy"):
        pattern = keys.conc_key(resource) + "*"
        async for key in r.scan_iter(match=pattern):
            existing.add(key)
    if existing:
        await r.delete(*existing)

    for node, (cpu, mem_mb, io_heavy) in totals.items():
        node_id = node or None
        await r.mset(
            {
                keys.conc_key("cpu", node_id): cpu,
                keys.conc_key("mem_mb", node_id): mem_mb,
                keys.conc_key("io_heavy", node_id): io_heavy,
            }
        )

    return len(job_ids)


async def reconcile() -> int:
    """Rebuild Redis dispatch state from MongoDB.

    Run at startup. Without it, an AOF loss or a flushed Redis would silently
    strand every queued job -- they would still exist in Mongo, but nothing
    would ever dispatch them.

    It also resets the `bp:conc:*` reservation counters to the truth held in the
    RUNNING set (see `_rebuild_reservation_counters`), so a leaked reservation
    from a crashed or raced release does not shrink admission headroom forever.
    """
    r = get_redis()
    restored = 0
    now_ms = int(datetime.now(UTC).timestamp() * 1000)

    running_counted = await _rebuild_reservation_counters(r)

    async for job in Job.find({"state": {"$in": [s.value for s in ACTIVE_STATES]}}):
        job_id = str(job.id)
        if await r.zscore(keys.READY, job_id) is not None:
            continue
        if await r.zscore(keys.DELAYED, job_id) is not None:
            continue
        if job.state is JobState.RUNNING and await r.zscore(keys.RUNNING, job_id) is not None:
            continue

        if job.state is JobState.BLOCKED:
            # A blocked job has no Redis presence by design, so it is not
            # "missing" and must not be restored into the ready set -- doing so
            # would dispatch it ahead of the inputs it is waiting for.
            #
            # It does still need checking: its dependencies may have finished
            # while Redis was down, and the completion that would have released
            # it is long gone. Re-running the release decision here is what
            # stops a restart from stranding it forever.
            failed = await _failed_dependencies(job.depends_on)
            if failed:
                await _fail_blocked_job(job, failed)
            elif not await _unfinished_dependencies(job.depends_on):
                await job.set({Job.state: JobState.PENDING})
                await _push_to_redis(job)
                restored += 1
                log.info("blocked_job_released_on_reconcile", job_id=job_id, type=job.type)
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
                # Kept in step with _push_to_redis. This is the half that makes
                # the override survive a requeue: Mongo is the record of truth
                # and this is where the hash is rebuilt from it.
                "override": "1" if job.resource_override else "0",
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

    if restored or running_counted:
        log.info(
            "queue_reconciled",
            restored=restored,
            running_counted=running_counted,
            now_ms=now_ms,
        )
    return restored


async def publish_event(event_type: str, data: dict, *, owner: str) -> None:
    """Fan out to one owner's SSE subscribers via Redis pub/sub.

    Events are advisory: the UI refetches on receipt rather than treating the
    payload as authoritative, so a dropped message costs a delay, not accuracy.
    That is also what makes per-owner channels the safe design -- see
    `keys.events_channel`.

    `owner` is keyword-only and has no default on purpose. It is the whole
    enforcement mechanism: a new publisher that has not thought about which
    profile its event belongs to fails to call this at all, rather than
    defaulting into someone's stream. Installation-wide events pass
    `keys.SYSTEM_OWNER` explicitly, which is a decision the author had to make;
    omitting an argument is not.
    """
    try:
        await get_redis().publish(
            keys.events_channel(owner), json.dumps({"type": event_type, "data": data})
        )
    except Exception as e:  # noqa: BLE001 - never fail a job over telemetry
        log.debug("event_publish_failed", error=str(e))
