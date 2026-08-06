"""Jobs: the durable record of queued work.

Redis is the dispatch substrate; this collection is the record of truth. If
Redis is lost entirely, `reconcile_queue` rebuilds the ready/delayed sets from
these documents.
"""

from datetime import datetime
from enum import StrEnum

from beanie import PydanticObjectId
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.models.base import TimestampedDocument


class JobClass(StrEnum):
    """Priority tier. Lower dispatch score wins; see queue/priority.py."""

    USER_INTERACTIVE = "user_interactive"  # the user is watching
    USER_BACKGROUND = "user_background"  # follow-up to the user's own action
    MAINTENANCE = "maintenance"  # verification, GC
    # Pipeline execution: trimming, alignment. Deliberately below maintenance
    # and never promoted -- a multi-hour fastp run that ages into the
    # user-interactive tier would be exactly backwards.
    COMPUTE = "compute"
    BULK = "bulk"  # whole-library sweeps


class JobState(StrEnum):
    PENDING = "pending"  # written to Mongo, not yet in Redis
    QUEUED = "queued"
    DELAYED = "delayed"  # awaiting backoff or a scheduled time
    # Held until every job in `depends_on` has succeeded. Distinct from DELAYED,
    # which is a timer: a blocked job has no scheduled release time and is
    # freed by an event (its last dependency finishing) rather than the clock.
    # Conflating them would make the delayed-promotion sweep dispatch a job
    # whose inputs do not exist yet.
    BLOCKED = "blocked"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEAD = "dead"  # exhausted max_attempts


TERMINAL_STATES = {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED, JobState.DEAD}
ACTIVE_STATES = {
    JobState.PENDING,
    JobState.QUEUED,
    JobState.DELAYED,
    JobState.BLOCKED,
    JobState.RUNNING,
}


class IoClass(StrEnum):
    NONE = "none"
    LIGHT = "light"
    # More than a couple of concurrent heavy readers on a FUSE mount is slower
    # in aggregate than two, so this is a throughput cap as well as a safety one.
    HEAVY = "heavy"


class JobResources(BaseModel):
    """Declared demand, reserved atomically at claim time."""

    cpu: int = 1
    mem_mb: int = 256
    io: IoClass = IoClass.NONE


class JobLease(BaseModel):
    worker_id: str
    expires_at: datetime
    heartbeat_at: datetime
    # Fencing token. Docker Desktop's VM pauses when the laptop lid closes, so a
    # lease can expire while its job is genuinely still running and another
    # worker picks the job up. Every write-back is conditional on this epoch, so
    # the resumed zombie's writes are rejected instead of corrupting state.
    epoch: int = 0


class JobProgress(BaseModel):
    # None means indeterminate, not zero: a tool that cannot produce an honest
    # fraction (Flye, Clair3, minimap2 -- see assembly_runner.py:83) reports
    # phases only, and a bar rendered at 0% for its whole run is
    # indistinguishable from a stalled job.
    pct: float | None = None
    phase: str = ""
    bytes_done: int = 0
    bytes_total: int = 0
    message: str = ""
    # Generic countable units -- reads, contigs, chunks, records -- for
    # progress that bytes cannot express. bytes_done/bytes_total stay as they
    # are (hashing, chunk assembly) since a size renders differently from a
    # count; unit_label is free text because the vocabulary is per tool.
    units_done: int | None = None
    units_total: int | None = None
    unit_label: str = ""
    # Current and running-peak resource use, sampled once a second from the
    # job's process subtree (queue/resource_sampler.py). Current answers "what
    # is the machine doing now"; peak answers "did this already touch the
    # ceiling", the question asked after an unexplained failure. Deliberately
    # not gated by RESOURCE_FLOOR_MS -- that floor exists because job_timings
    # feeds a model that a handful of samples would bias, and a number
    # displayed live is not an input to anything.
    rss_bytes: int | None = None
    cpu_percent: float | None = None
    peak_rss_bytes: int | None = None
    peak_cpu_percent: float | None = None
    # "Step 2 of 5" -- only where a runner can declare its phase list up
    # front, which every runner now can: fastp and align_runner from a flat
    # constant, assembly_runner from `flye_stage_order(params)`, since Flye
    # builds its whole job list at launch and only `--iterations 0` varies it.
    # Both null still means "unstructured -- render the phase name alone",
    # which is the correct representation for a stage no runner declared
    # (a future Flye adding one), not a placeholder.
    phase_index: int | None = None
    phase_total: int | None = None


class AttemptProgress(BaseModel):
    """The previous attempt's progress, kept as a high-water mark.

    A job that died mid-run and got requeued must not come back claiming
    whatever pct it last reported -- it is restarting from zero. But that
    number is the most useful thing available about a job that keeps dying:
    "attempt 2; attempt 1 reached 80% at 'assembly', peaking at 14.2 GB" is
    the shape of a job hitting the same OOM every time. Only the previous
    attempt is kept, not a history -- the comparison that matters is against
    the last one, and an unbounded array on a hot document to answer a rarer
    question is not the trade to make here.

    Lives on `Job`, not nested inside `JobProgress`: `JobProgress` describes
    the *current* attempt, and nesting the previous one inside it invites
    code that reads a percentage without noticing which attempt it belongs
    to.
    """

    attempt: int
    pct: float | None = None
    phase: str = ""
    message: str = ""
    peak_rss_bytes: int | None = None


class JobError(BaseModel):
    code: str
    message: str
    traceback_tail: str = ""
    retryable: bool = True


class JobTiming(BaseModel):
    enqueued_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None


class Job(TimestampedDocument):
    type: str  # handler name, see queue/registry.py
    job_class: JobClass = JobClass.USER_BACKGROUND
    state: JobState = JobState.PENDING
    payload: dict = Field(default_factory=dict)

    # Prevents enqueueing the same logical work twice. Enforced by a unique
    # partial index over non-terminal states -- Redis is the fast path, this is
    # the guarantee that survives a Redis flush.
    dedup_key: str | None = None

    project_id: PydanticObjectId | None = None
    object_id: PydanticObjectId | None = None

    attempts: int = 0
    max_attempts: int = 5
    available_at: datetime | None = None  # for delayed/backoff

    lease: JobLease | None = None
    progress: JobProgress = Field(default_factory=JobProgress)
    # Set once, in mark_running, when a job that already had progress starts
    # a later attempt. None on a first attempt. A terminal failure leaves this
    # untouched -- a failed job's own `progress` already shows what it was
    # doing when it died, which is more useful than moving it here.
    last_attempt_progress: AttemptProgress | None = None
    cancel_requested: bool = False

    result: dict | None = None
    error: JobError | None = None
    timing: JobTiming = Field(default_factory=JobTiming)
    resources: JobResources = Field(default_factory=JobResources)

    parent_job_id: PydanticObjectId | None = None  # the job that enqueued this one

    # Jobs that must succeed before this one may dispatch. A list rather than a
    # single id because a step can need several independent inputs -- aligning
    # against a reference that needs both an aligner index and a .fai is the
    # case that exists today, and neither ordering between them is meaningful.
    #
    # Enforced in MongoDB rather than in claim.lua: a blocked job is simply
    # never pushed to Redis, so it is not a dispatch candidate at all. Teaching
    # the claim script to skip blocked jobs would put a per-candidate lookup on
    # the hot path to enforce something the enqueue side already knows.
    depends_on: list[PydanticObjectId] = Field(default_factory=list)

    # TTL field: terminal jobs are pruned after ~30 days. Only set on completion,
    # so active jobs are never eligible for expiry.
    expires_at: datetime | None = None

    class Settings:
        name = "jobs"
        indexes = [
            IndexModel([("state", ASCENDING), ("available_at", ASCENDING)], name="dispatchable"),
            # The durable duplicate-enqueue guard. The $type clause is required:
            # a plain unique index treats every missing/null dedup_key as the
            # same value, so jobs that opt out of deduplication would collide
            # with each other.
            IndexModel(
                [("dedup_key", ASCENDING)],
                name="uniq_active_dedup_key",
                unique=True,
                partialFilterExpression={
                    "dedup_key": {"$type": "string"},
                    # Must list every non-terminal state, "blocked" included:
                    # a state missing here is one where the same logical work
                    # could be enqueued twice.
                    "state": {"$in": ["pending", "queued", "delayed", "blocked", "running"]},
                },
            ),
            IndexModel(
                [("project_id", ASCENDING), ("created_at", DESCENDING)], name="by_project"
            ),
            IndexModel([("object_id", ASCENDING), ("created_at", DESCENDING)], name="by_object"),
            # "Which blocked jobs was this one holding up?" -- the reverse
            # lookup `complete` runs on every terminal outcome. Multikey over
            # the array, so a finished job finds its dependents by its own id.
            IndexModel([("depends_on", ASCENDING)], name="by_depends_on"),
            # Reaper scan: only running jobs have leases.
            IndexModel(
                [("lease.expires_at", ASCENDING)],
                name="lease_expiry",
                partialFilterExpression={"state": "running"},
            ),
            IndexModel([("expires_at", ASCENDING)], name="ttl", expireAfterSeconds=0),
        ]
