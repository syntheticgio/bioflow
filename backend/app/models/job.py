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
    BULK = "bulk"  # whole-library sweeps


class JobState(StrEnum):
    PENDING = "pending"  # written to Mongo, not yet in Redis
    QUEUED = "queued"
    DELAYED = "delayed"  # awaiting backoff or a scheduled time
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEAD = "dead"  # exhausted max_attempts


TERMINAL_STATES = {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED, JobState.DEAD}
ACTIVE_STATES = {JobState.PENDING, JobState.QUEUED, JobState.DELAYED, JobState.RUNNING}


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
    pct: float = 0.0
    phase: str = ""
    bytes_done: int = 0
    bytes_total: int = 0
    message: str = ""


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
    cancel_requested: bool = False

    result: dict | None = None
    error: JobError | None = None
    timing: JobTiming = Field(default_factory=JobTiming)
    resources: JobResources = Field(default_factory=JobResources)

    parent_job_id: PydanticObjectId | None = None  # pipeline DAG, Phase 6

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
                    "state": {"$in": ["pending", "queued", "delayed", "running"]},
                },
            ),
            IndexModel(
                [("project_id", ASCENDING), ("created_at", DESCENDING)], name="by_project"
            ),
            IndexModel([("object_id", ASCENDING), ("created_at", DESCENDING)], name="by_object"),
            # Reaper scan: only running jobs have leases.
            IndexModel(
                [("lease.expires_at", ASCENDING)],
                name="lease_expiry",
                partialFilterExpression={"state": "running"},
            ),
            IndexModel([("expires_at", ASCENDING)], name="ttl", expireAfterSeconds=0),
        ]
