"""Schedules: periodic job definitions (exercised in Phase 4)."""

from datetime import datetime

from beanie import PydanticObjectId
from pydantic import Field

from app.models.base import TimestampedDocument
from app.models.job import JobClass


class Schedule(TimestampedDocument):
    # The schedule name is the primary key.
    id: str  # type: ignore[assignment]

    job_type: str
    interval_seconds: int
    job_class: JobClass = JobClass.MAINTENANCE
    payload: dict = Field(default_factory=dict)
    enabled: bool = True
    jitter_seconds: int = 0

    # Never backfill missed ticks. The Docker VM pauses when the laptop sleeps,
    # so a 4-hour sleep must produce one tick on resume, not 240.
    catchup: bool = False

    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    last_job_id: PydanticObjectId | None = None

    class Settings:
        name = "schedules"
