"""Pipeline runs: creating them, linking jobs, and describing their state.

The status derivation is the part worth care. It is pure -- a function over
(role, state) pairs rather than over documents -- because the interesting cases
are orderings that are awkward to reproduce against a live queue: a run whose
index build failed while its alignment was still blocked, a run whose jobs have
been pruned by the TTL, a run that produced its output but failed to parse it.
"""

from beanie import PydanticObjectId
from pymongo.errors import DuplicateKeyError

from app.logging import get_logger
from app.models import (
    OPTIONAL_ROLES,
    Job,
    JobState,
    PipelineRun,
    RunInput,
    RunJob,
    RunJobRole,
    RunKind,
    RunStatus,
)

log = get_logger(__name__)

_UNSUCCESSFUL = {JobState.FAILED, JobState.DEAD, JobState.CANCELLED}
_IN_FLIGHT = {JobState.PENDING, JobState.QUEUED, JobState.DELAYED, JobState.BLOCKED}


def derive_status(
    members: list[tuple[RunJobRole, JobState | None]],
    *,
    optional_roles: frozenset[RunJobRole] = OPTIONAL_ROLES,
) -> RunStatus:
    """A run's status, from the roles and states of its member jobs.

    `None` for a state means the job no longer exists -- pruned by the 30-day
    TTL. It counts as succeeded: the alternative is a run that spontaneously
    reports failure a month later, which would be a lie about something that
    worked.

    Precedence matters and is deliberate. Failure of a required member wins
    over everything, including members still running: the run is already doomed
    and saying so immediately beats waiting for a sibling whose work is now
    wasted. That mirrors how the queue's dependency gate fails a dependent as
    soon as any dependency fails.
    """
    if not members:
        # A run whose jobs were all deduplicated away, or that was created and
        # never linked. Nothing failed, so nothing is wrong.
        return RunStatus.SUCCEEDED

    required = [(r, s) for r, s in members if r not in optional_roles]
    optional = [(r, s) for r, s in members if r in optional_roles]

    if any(s in _UNSUCCESSFUL for _, s in required):
        return RunStatus.FAILED
    if any(s is JobState.RUNNING for _, s in members):
        return RunStatus.RUNNING
    if any(s in _IN_FLIGHT for _, s in members):
        return RunStatus.WAITING
    if any(s in _UNSUCCESSFUL for _, s in optional):
        # The expensive work succeeded and its output exists; only a follow-up
        # step failed. Saying FAILED would overstate it and saying SUCCEEDED
        # would hide it.
        return RunStatus.PARTIAL
    return RunStatus.SUCCEEDED


async def create_run(
    *,
    kind: RunKind,
    project_id: PydanticObjectId,
    label: str,
    inputs: list[RunInput],
    params: dict,
    tool: str | None = None,
) -> PipelineRun:
    """Record what a user asked for, before any of it is enqueued."""
    run = PipelineRun(
        kind=kind,
        project_id=project_id,
        label=label,
        inputs=inputs,
        params=params,
        tool=tool,
    )
    await run.insert()
    log.info("run_created", run_id=str(run.id), kind=kind.value, label=label)
    return run


async def discard_run(run_id: PydanticObjectId) -> None:
    """Delete a run whose work was deduplicated away before it started.

    Only for the launch path: a run that turns out to describe nothing must not
    linger in the activity view implying work is happening. Its membership rows
    go too, but the *jobs* they referenced are untouched -- a shared index build
    is real work owned by whichever run queued it first.
    """
    await RunJob.find(RunJob.run_id == run_id).delete()
    run = await PipelineRun.get(run_id)
    if run is not None:
        await run.delete()
    log.info("run_discarded", run_id=str(run_id))


async def link_job(
    run_id: PydanticObjectId,
    job_id: PydanticObjectId,
    role: RunJobRole,
    *,
    shared: bool = False,
) -> None:
    """Record that a job serves a run.

    Idempotent by way of the unique index: a retry that re-links the same job
    must not create a second member, which would double-count it in the
    derived status.
    """
    try:
        await RunJob(run_id=run_id, job_id=job_id, role=role, shared=shared).insert()
    except DuplicateKeyError:
        log.debug("run_job_already_linked", run_id=str(run_id), job_id=str(job_id))


async def run_for_job(job_id: PydanticObjectId) -> PydanticObjectId | None:
    """The run a job belongs to, if any.

    How a job enqueued *after* launch joins its run: `index_bam` is enqueued
    from the alignment's applier because it needs the BAM's digest, and an
    ingest is enqueued from `ingest_local_file`. Both resolve the run from the
    job that caused them.
    """
    link = await RunJob.find_one(RunJob.job_id == job_id)
    return link.run_id if link else None


async def members(run_id: PydanticObjectId) -> list[RunJob]:
    return await RunJob.find(RunJob.run_id == run_id).to_list()


async def status_for(run_id: PydanticObjectId) -> tuple[RunStatus, list[dict]]:
    """A run's derived status and the state of each member job.

    Returns both because every caller that wants one wants the other, and
    fetching the jobs is the expensive half.
    """
    links = await members(run_id)
    if not links:
        return RunStatus.SUCCEEDED, []

    jobs = await Job.find({"_id": {"$in": [link.job_id for link in links]}}).to_list()
    by_id = {job.id: job for job in jobs}

    detail: list[dict] = []
    pairs: list[tuple[RunJobRole, JobState | None]] = []
    for link in links:
        job = by_id.get(link.job_id)
        pairs.append((link.role, job.state if job else None))
        detail.append(
            {
                "job_id": str(link.job_id),
                "role": link.role.value,
                "shared": link.shared,
                # Absent when the job has been pruned. The UI says "expired"
                # rather than inventing a state.
                "type": job.type if job else None,
                "state": job.state.value if job else None,
                "progress": job.progress.model_dump(mode="json") if job else None,
                "error": job.error.model_dump(mode="json") if job and job.error else None,
                "created_at": job.created_at if job else None,
            }
        )

    detail.sort(key=lambda d: (d["created_at"] is None, d["created_at"]))
    return derive_status(pairs), detail


async def status_for_many(
    run_ids: list[PydanticObjectId],
) -> dict[PydanticObjectId, RunStatus]:
    """Derived status for several runs, in two queries rather than 2N.

    The listing renders every run's status, and doing that one run at a time
    would put the cost of the activity view on the number of runs -- the exact
    shape that makes a page feel slow as history accumulates.
    """
    if not run_ids:
        return {}

    links = await RunJob.find({"run_id": {"$in": run_ids}}).to_list()
    if not links:
        return {rid: RunStatus.SUCCEEDED for rid in run_ids}

    jobs = await Job.find({"_id": {"$in": [link.job_id for link in links]}}).to_list()
    states = {job.id: job.state for job in jobs}

    by_run: dict[PydanticObjectId, list[tuple[RunJobRole, JobState | None]]] = {
        rid: [] for rid in run_ids
    }
    for link in links:
        by_run.setdefault(link.run_id, []).append(
            (link.role, states.get(link.job_id))
        )

    return {rid: derive_status(pairs) for rid, pairs in by_run.items()}


async def record_outputs(
    run_id: PydanticObjectId, object_ids: list[PydanticObjectId]
) -> None:
    """Attach produced objects to the run that made them."""
    if not object_ids:
        return
    run = await PipelineRun.get(run_id)
    if run is None:
        return
    existing = set(run.outputs)
    merged = run.outputs + [oid for oid in object_ids if oid not in existing]
    run.outputs = merged
    run.touch()
    await run.save()


async def link_job_to_run_of(
    *, cause_job_id: str | None, job_id: PydanticObjectId, role: RunJobRole
) -> None:
    """Link a job to whichever run the job that caused it belongs to.

    A job whose run cannot be resolved is simply not linked. Grouping is a
    presentation concern, and failing a pipeline over it would be absurd.
    """
    if not cause_job_id:
        return
    try:
        run_id = await run_for_job(PydanticObjectId(cause_job_id))
    except Exception as e:  # noqa: BLE001 - never fail real work over grouping
        log.debug("run_lookup_failed", job_id=cause_job_id, error=str(e))
        return
    if run_id is not None:
        await link_job(run_id, job_id, role)
