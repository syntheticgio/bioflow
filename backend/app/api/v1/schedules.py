"""Periodic schedule endpoints."""

from datetime import datetime

from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from app.errors import NotFoundError, ValidationError
from app.models import Schedule
from app.queue import scheduler

router = APIRouter(prefix="/schedules", tags=["schedules"])


class ScheduleOut(BaseModel):
    name: str
    job_type: str
    interval_seconds: int
    job_class: str
    payload: dict
    enabled: bool
    catchup: bool
    last_run_at: datetime | None
    next_run_at: datetime | None
    last_job_id: str | None

    @classmethod
    def of(cls, s: Schedule) -> "ScheduleOut":
        return cls(
            name=s.id,
            job_type=s.job_type,
            interval_seconds=s.interval_seconds,
            job_class=s.job_class.value,
            payload=s.payload,
            enabled=s.enabled,
            catchup=s.catchup,
            last_run_at=s.last_run_at,
            next_run_at=s.next_run_at,
            last_job_id=str(s.last_job_id) if s.last_job_id else None,
        )


class ScheduleUpdate(BaseModel):
    enabled: bool | None = None
    interval_seconds: int | None = Field(None, ge=5, le=86400)
    payload: dict | None = None


@router.get("", response_model=list[ScheduleOut])
async def list_schedules() -> list[ScheduleOut]:
    return [ScheduleOut.of(s) for s in await Schedule.find().to_list()]


@router.get("/overdue")
async def list_overdue() -> dict:
    """Schedules that have not run in far longer than their interval.

    A verify_files that quietly stopped running is invisible until the moment
    you need the data, so it is surfaced rather than left to be noticed.
    """
    return {"overdue": await scheduler.overdue()}


@router.get("/{name}", response_model=ScheduleOut)
async def get_schedule(name: str) -> ScheduleOut:
    schedule = await Schedule.get(name)
    if schedule is None:
        raise NotFoundError(f"Schedule not found: {name}")
    return ScheduleOut.of(schedule)


@router.patch("/{name}", response_model=ScheduleOut)
async def update_schedule(name: str, body: ScheduleUpdate) -> ScheduleOut:
    schedule = await Schedule.get(name)
    if schedule is None:
        raise NotFoundError(f"Schedule not found: {name}")

    updates = body.model_dump(exclude_unset=True)
    interval_changed = (
        "interval_seconds" in updates
        and updates["interval_seconds"] != schedule.interval_seconds
    )

    for field, value in updates.items():
        setattr(schedule, field, value)
    schedule.touch()
    await schedule.save()

    # The next fire time lives in Redis; without clearing it a shortened
    # interval would not take effect until the old one elapsed.
    if interval_changed:
        await scheduler.reset_next_run(name)

    return ScheduleOut.of(schedule)


@router.post("/{name}/run-now", status_code=status.HTTP_202_ACCEPTED)
async def run_schedule_now(name: str) -> dict:
    """Fire one run immediately, at user priority."""
    job_id = await scheduler.run_now(name)
    if job_id is None:
        if await Schedule.get(name) is None:
            raise NotFoundError(f"Schedule not found: {name}")
        raise ValidationError("An identical run is already queued")
    return {"name": name, "job_id": job_id}
