"""Queue statistics for /system/stats and the footer badge."""

from app.db.client import get_db
from app.db.redis_client import get_redis
from app.models import JobState
from app.queue import keys, worker_registry


async def snapshot() -> dict:
    r = get_redis()
    pipe = r.pipeline()
    pipe.zcard(keys.READY)
    pipe.zcard(keys.DELAYED)
    pipe.zcard(keys.RUNNING)
    ready, delayed, running = await pipe.execute()

    # Via the registry rather than a raw hgetall: the hash retains dead
    # workers' last heartbeats, which would inflate the footer's count the
    # same way they inflated the node table (#451).
    workers = [
        {"id": worker_id, **info}
        for worker_id, info in await worker_registry.live_workers()
    ]

    # Grouped server-side: iterating documents just to count them would scale
    # with queue depth on an endpoint the footer polls continuously.
    active = [
        JobState.QUEUED.value,
        JobState.DELAYED.value,
        JobState.BLOCKED.value,
        JobState.RUNNING.value,
    ]
    by_class = {
        row["_id"]: row["count"]
        async for row in await get_db().jobs.aggregate(
            [
                {"$match": {"state": {"$in": active}}},
                {"$group": {"_id": "$job_class", "count": {"$sum": 1}}},
            ]
        )
    }

    return {
        "ready": ready,
        "delayed": delayed,
        "running": running,
        "by_class": by_class,
        "workers": len(workers),
        "worker_detail": workers,
    }
