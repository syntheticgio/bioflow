"""Job endpoints."""

from datetime import UTC, datetime

from beanie import PydanticObjectId
from fastapi import APIRouter, Query, status
from pydantic import BaseModel, Field

from app.errors import NotFoundError, ValidationError
from app.models import Job, JobClass, JobState
from app.queue import queue
from app.queue.registry import all_handlers, get_handler

router = APIRouter(prefix="/jobs", tags=["jobs"])


class JobOut(BaseModel):
    id: str
    type: str
    job_class: str
    state: str
    payload: dict
    attempts: int
    max_attempts: int
    progress: dict
    result: dict | None
    error: dict | None
    timing: dict
    resources: dict
    cancel_requested: bool
    project_id: str | None
    object_id: str | None
    parent_job_id: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, j: Job) -> "JobOut":
        return cls(
            id=str(j.id),
            type=j.type,
            job_class=j.job_class.value,
            state=j.state.value,
            payload=j.payload,
            attempts=j.attempts,
            max_attempts=j.max_attempts,
            progress=j.progress.model_dump(mode="json"),
            result=j.result,
            error=j.error.model_dump(mode="json") if j.error else None,
            timing=j.timing.model_dump(mode="json"),
            resources=j.resources.model_dump(mode="json"),
            cancel_requested=j.cancel_requested,
            project_id=str(j.project_id) if j.project_id else None,
            object_id=str(j.object_id) if j.object_id else None,
            parent_job_id=str(j.parent_job_id) if j.parent_job_id else None,
            created_at=j.created_at,
            updated_at=j.updated_at,
        )


class JobCreate(BaseModel):
    type: str
    payload: dict = Field(default_factory=dict)
    job_class: JobClass | None = None
    delay_seconds: float = 0
    dedup_key: str | None = None


def _parse_states(raw: str) -> list[str]:
    """Expand a states filter into state values.

    `active` is spelled out rather than left to the caller because the set of
    non-terminal states is the queue's business, and a UI that hardcoded it
    would silently miss a state added later.
    """
    from app.models.job import ACTIVE_STATES

    wanted: list[str] = []
    for part in (p.strip() for p in raw.split(",")):
        if not part:
            continue
        if part == "active":
            wanted.extend(sorted(s.value for s in ACTIVE_STATES))
            continue
        try:
            wanted.append(JobState(part).value)
        except ValueError:
            raise ValidationError(
                f"Unknown job state: {part!r}",
                details={"known": [s.value for s in JobState] + ["active"]},
            ) from None
    return sorted(set(wanted))


@router.get("", response_model=list[JobOut])
async def list_jobs(
    state: JobState | None = None,
    states: str | None = Query(
        None,
        description=(
            "Comma-separated states, or the alias 'active'. Lets the activity "
            "view fetch everything in flight in one request rather than one "
            "call per state."
        ),
    ),
    job_type: str | None = Query(None, alias="type"),
    job_class: JobClass | None = Query(None, alias="class"),
    project_id: str | None = None,
    object_id: str | None = Query(
        None,
        description=(
            "Jobs launched against one file. Backed by the by_object index, so "
            "the detail panel can ask 'is anything running on this?' without "
            "scanning the queue."
        ),
    ),
    limit: int = Query(100, le=500),
) -> list[JobOut]:
    query: dict = {}
    if states:
        query["state"] = {"$in": _parse_states(states)}
    elif state:
        query["state"] = state.value
    if job_type:
        query["type"] = job_type
    if job_class:
        query["job_class"] = job_class.value
    if project_id:
        query["project_id"] = PydanticObjectId(project_id)
    if object_id:
        query["object_id"] = PydanticObjectId(object_id)

    jobs = await Job.find(query).sort("-created_at").limit(limit).to_list()
    return [JobOut.of(j) for j in jobs]


@router.post("", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def create_job(body: JobCreate) -> JobOut:
    """Enqueue a job directly. Primarily for development and smoke testing."""
    spec = get_handler(body.type)
    if spec is None:
        raise ValidationError(
            f"Unknown job type: {body.type!r}",
            details={"known_types": sorted(all_handlers())},
        )

    job = await queue.enqueue(
        body.type,
        # TODO(profiles): route wiring is Task 10 -- once this endpoint takes
        # the OwnerDep, pass the caller's owner instead of the literal. It is a
        # dev/smoke-test endpoint, so nothing user-facing depends on the gap.
        owner="local",
        payload=body.payload,
        job_class=body.job_class or spec.default_class,
        resources=spec.default_resources,
        max_attempts=spec.max_attempts,
        dedup_key=body.dedup_key,
        delay_seconds=body.delay_seconds,
    )
    if job is None:
        raise ValidationError(
            "An identical job is already queued",
            details={"dedup_key": body.dedup_key},
        )
    return JobOut.of(job)


@router.get("/timing-model")
async def timing_model() -> dict:
    """Per-job-type duration models, and how many samples back each one."""
    from app.services import timing_service

    return {
        "min_samples": timing_service.MIN_SAMPLES,
        "types": await timing_service.stats(),
    }


@router.get("/types")
async def list_job_types() -> dict:
    return {
        name: {
            "mode": spec.mode.value,
            "default_class": spec.default_class.value,
            "resources": spec.default_resources.model_dump(mode="json"),
        }
        for name, spec in all_handlers().items()
    }


@router.get("/{job_id}")
async def get_job(job_id: PydanticObjectId) -> dict:
    """Job detail, plus a duration estimate when enough history exists."""
    job = await Job.get(job_id)
    if job is None:
        raise NotFoundError(f"Job not found: {job_id}")

    out = JobOut.of(job).model_dump(mode="json")

    # Only worth predicting while the job is still running -- afterwards the
    # actual duration is the better number.
    if job.state in (JobState.RUNNING, JobState.QUEUED, JobState.PENDING):
        from app.services import timing_service

        size = job.payload.get("size") or 0
        if not size and job.object_id:
            from app.models import DataObject

            obj = await DataObject.get(job.object_id)
            size = obj.size if obj else 0
        if size:
            out["timing_estimate"] = await timing_service.estimate(job.type, size)
    return out


@router.post("/{job_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_job(job_id: PydanticObjectId) -> dict:
    """Request cancellation.

    Cancellation of a running job is cooperative, so this returns the
    disposition rather than pretending the job stopped instantly.
    """
    outcome = await queue.request_cancel(str(job_id))
    if outcome == "not_found":
        raise NotFoundError(f"Job not found: {job_id}")
    return {"job_id": str(job_id), "outcome": outcome}


@router.post("/{job_id}/retry", response_model=JobOut)
async def retry_job(job_id: PydanticObjectId) -> JobOut:
    """Resurrect a failed or dead job with a fresh attempt budget."""
    job = await Job.get(job_id)
    if job is None:
        raise NotFoundError(f"Job not found: {job_id}")
    if job.state not in (JobState.FAILED, JobState.DEAD, JobState.CANCELLED):
        raise ValidationError(
            f"Only failed, dead or cancelled jobs can be retried (state={job.state.value})"
        )

    await job.set(
        {
            Job.state: JobState.PENDING,
            Job.attempts: 0,
            Job.error: None,
            Job.cancel_requested: False,
            Job.expires_at: None,
            Job.updated_at: datetime.now(UTC),
        }
    )
    refreshed = await Job.get(job_id)
    await queue._push_to_redis(refreshed)  # type: ignore[arg-type]
    return JobOut.of(await Job.get(job_id))  # type: ignore[arg-type]


# Bounded so a request can never pull an unbounded file into memory: a fastp
# run on a large library writes a lot, and this endpoint is polled.
MAX_LOG_TAIL_LINES = 2000
LOG_READ_BYTES = 256 * 1024


@router.get("/{job_id}/log")
async def get_job_log(
    job_id: PydanticObjectId,
    tail: int = Query(200, ge=1, le=MAX_LOG_TAIL_LINES),
) -> dict:
    """The tail of a job's captured output.

    Only jobs that shell out to an external tool write one, so an absent log is
    a normal answer rather than an error -- most job types have nothing to say.
    """
    import asyncio

    from app.config import settings

    job = await Job.get(job_id)
    if job is None:
        raise NotFoundError(f"Job not found: {job_id}")

    path = settings.logs_dir / f"{job_id}.log"
    if not await asyncio.to_thread(path.exists):
        return {"job_id": str(job_id), "exists": False, "lines": [], "truncated": False}

    lines, truncated, size = await asyncio.to_thread(_read_tail, path, tail)
    return {
        "job_id": str(job_id),
        "exists": True,
        "lines": lines,
        "truncated": truncated,
        "size": size,
    }


def _read_tail(path, tail: int) -> tuple[list[str], bool, int]:
    """Last `tail` lines, reading only the end of the file.

    Seeks rather than reading the whole file: a long-running job's log can
    reach hundreds of megabytes, and this endpoint is polled while the job runs.
    """
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            offset = max(0, size - LOG_READ_BYTES)
            f.seek(offset)
            chunk = f.read()
    except OSError:
        return [], False, 0

    text = chunk.decode("utf-8", errors="replace")
    # A mid-line seek leaves a partial first line; drop it rather than showing
    # a fragment that looks like real output.
    if offset > 0 and "\n" in text:
        text = text.split("\n", 1)[1]

    all_lines = text.splitlines()
    return all_lines[-tail:], (offset > 0 or len(all_lines) > tail), size
