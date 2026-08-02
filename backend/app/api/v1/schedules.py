"""Periodic schedule endpoints.

Deliberately *not* owner-scoped, unlike almost every other router here. The
schedules are the installation's own maintenance -- garbage collection and file
verification -- and `queue/scheduler.py` already says so in code: both `tick`
and `run_now` enqueue with `owner=keys.SYSTEM_OWNER` under a comment reading
that this work "runs against the whole installation, not any one profile's
library, so there is no owner to inherit here and there never will be."

Scoping these five routes per profile would therefore produce one of two
useless outcomes: every profile seeing an empty list, or five identical copies
of a single cron table that any of them could edit for all the others.

The line to hold is the one `search.py`'s `/metadata/schemas` docstring draws:
does the route read user data? These do not -- a `Schedule` document describes
when the machine sweeps its own storage. The search, facet, metadata-value and
bulk-edit routes all do, and are scoped.

A per-profile scheduled task, if one is ever wanted, is a different feature
rather than a fix to this one. It would need its own `owner` field on
`Schedule`, an owner threaded through `scheduler.tick`'s `enqueue`, and these
routes filtering on it. That door is open; nothing here has quietly shut it.

One consequence worth knowing when reading the UI: because maintenance jobs
carry `owner: "system"`, they appear in *every* profile's job list and event
stream rather than in one profile's. That is deliberate, and it is what makes
these global routes honest -- a schedule any profile can edit and fire should
not produce a job only one of them can watch.

It was not always so. These jobs used to carry a hardcoded `owner: "local"`,
which reads as "the installation" but is really the owner string of whichever
profile adopted the pre-profiles library, so they landed in exactly one real
person's queue and a second profile pressing Run now watched the job vanish.
See docs/TODO.md, "Maintenance jobs belong to whichever profile adopted
'local'".
"""

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
