"""Periodic job scheduling.

Every worker ticks, but only one wins each interval: the claim is an atomic
compare-and-advance inside a Lua script, so there is no window between "is it
due?" and "take it". A SETNX-with-TTL approach leaves exactly that gap, and it
shows up as the same maintenance job enqueued two or three times.

`catchup=false` throughout: the Docker VM pauses when the laptop lid closes, so
a four-hour sleep must produce one tick on resume, not 240.
"""

from datetime import UTC, datetime

from app.db.redis_client import get_redis, get_script
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources, Schedule
from app.queue import keys

log = get_logger(__name__)

# Seeded on first startup. Editable at runtime through /schedules.
DEFAULT_SCHEDULES = [
    {
        "_id": "verify_files",
        "job_type": "verify_files",
        "interval_seconds": 60,
        "job_class": JobClass.MAINTENANCE,
        # A batch, not the whole library: see the verify_files docstring.
        "payload": {"batch_size": 500},
    },
    {
        "_id": "gc_blobs",
        "job_type": "gc_blobs",
        "interval_seconds": 600,
        "job_class": JobClass.MAINTENANCE,
        "payload": {"limit": 100},
    },
    {
        "_id": "reap_uploads",
        "job_type": "reap_uploads",
        "interval_seconds": 300,
        "job_class": JobClass.MAINTENANCE,
        "payload": {"max_age_hours": 24},
    },
    {
        "_id": "reap_report_dirs",
        # Hourly, matching its grace window. Deletion removes these inline, so
        # a sweep that finds anything is either cleaning up pre-existing strays
        # or covering a delete that failed partway -- neither is urgent.
        "job_type": "reap_report_dirs",
        "interval_seconds": 3600,
        "job_class": JobClass.MAINTENANCE,
        "payload": {"max_age_hours": 1},
    },
    {
        "_id": "reap_pipeline_scratch",
        "job_type": "reap_pipeline_scratch",
        # Hourly: the scratch it reclaims is whole FASTQ files, but a grace
        # period measured in hours means a shorter interval would find nothing.
        "interval_seconds": 3600,
        "job_class": JobClass.MAINTENANCE,
        "payload": {"scratch_grace_hours": 6},
    },
]

RESOURCES = {
    "verify_files": JobResources(cpu=1, mem_mb=64, io=IoClass.LIGHT),
    "gc_blobs": JobResources(cpu=1, mem_mb=64, io=IoClass.LIGHT),
    "reap_uploads": JobResources(cpu=1, mem_mb=64, io=IoClass.LIGHT),
    "reap_report_dirs": JobResources(cpu=1, mem_mb=64, io=IoClass.LIGHT),
    "reap_pipeline_scratch": JobResources(cpu=1, mem_mb=64, io=IoClass.LIGHT),
}


async def seed_defaults() -> int:
    """Create any missing default schedules. Never overwrites user edits."""
    created = 0
    for spec in DEFAULT_SCHEDULES:
        if await Schedule.get(spec["_id"]) is not None:
            continue
        await Schedule(
            id=spec["_id"],
            job_type=spec["job_type"],
            interval_seconds=spec["interval_seconds"],
            job_class=spec["job_class"],
            payload=spec["payload"],
            enabled=True,
            catchup=False,
        ).insert()
        created += 1
    if created:
        log.info("schedules_seeded", count=created)
    return created


async def tick() -> list[str]:
    """Fire any schedules that are due. Returns the job types enqueued."""
    from app.queue import queue as queue_mod

    fired: list[str] = []
    now = datetime.now(UTC)
    now_ms = int(now.timestamp() * 1000)

    async for schedule in Schedule.find(Schedule.enabled == True):  # noqa: E712
        interval_ms = schedule.interval_seconds * 1000

        won = await get_script("schedule_tick")(
            keys=[keys.sched_next_key(schedule.id)],
            args=[now_ms, interval_ms, "1" if schedule.catchup else "0"],
        )
        if not won:
            continue

        # The Mongo unique dedup index is the belt to Redis's braces: it
        # survives a Redis flush, which would otherwise re-fire everything.
        bucket = now_ms // interval_ms
        job = await queue_mod.enqueue(
            schedule.job_type,
            payload=schedule.payload,
            job_class=schedule.job_class,
            resources=RESOURCES.get(schedule.job_type, JobResources()),
            dedup_key=f"sched:{schedule.id}:{bucket}",
            max_attempts=2,
        )
        if job is None:
            continue

        await schedule.set(
            {
                Schedule.last_run_at: now,
                Schedule.last_job_id: job.id,
                Schedule.next_run_at: datetime.fromtimestamp(
                    (now_ms + interval_ms) / 1000, UTC
                ),
            }
        )
        fired.append(schedule.job_type)
        log.info("schedule_fired", name=schedule.id, job_id=str(job.id))

    return fired


async def run_now(name: str) -> str | None:
    """Force one run at user priority, bypassing the interval."""
    from app.queue import queue as queue_mod

    schedule = await Schedule.get(name)
    if schedule is None:
        return None

    job = await queue_mod.enqueue(
        schedule.job_type,
        payload=schedule.payload,
        # A human asked for this, so it should not queue behind maintenance.
        job_class=JobClass.USER_INTERACTIVE,
        resources=RESOURCES.get(schedule.job_type, JobResources()),
        dedup_key=f"sched:{name}:manual:{int(datetime.now(UTC).timestamp())}",
        max_attempts=2,
    )
    return str(job.id) if job else None


async def overdue() -> list[dict]:
    """Schedules that have not run in far longer than their interval.

    A `verify_files` that silently stops running is exactly the kind of failure
    nobody notices until they need the data, so it gets surfaced in the UI.
    """
    now = datetime.now(UTC)
    late = []
    async for s in Schedule.find(Schedule.enabled == True):  # noqa: E712
        if s.last_run_at is None:
            continue
        elapsed = (now - s.last_run_at).total_seconds()
        if elapsed > s.interval_seconds * 5:
            late.append(
                {
                    "name": s.id,
                    "interval_seconds": s.interval_seconds,
                    "last_run_at": s.last_run_at.isoformat(),
                    "seconds_overdue": int(elapsed - s.interval_seconds),
                }
            )
    return late


async def reset_next_run(name: str) -> None:
    """Clear the Redis tick marker so an interval change takes effect at once."""
    await get_redis().delete(keys.sched_next_key(name))
