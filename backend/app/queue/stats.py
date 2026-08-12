"""Queue statistics for /system/stats and the footer badge."""

import json

from app.db.client import get_db
from app.db.redis_client import get_redis
from app.models import JobState
from app.queue import keys


async def snapshot() -> dict:
    r = get_redis()
    pipe = r.pipeline()
    pipe.zcard(keys.READY)
    pipe.zcard(keys.DELAYED)
    pipe.zcard(keys.RUNNING)
    pipe.hgetall(keys.WORKERS)
    ready, delayed, running, workers_raw = await pipe.execute()

    workers = []
    for worker_id, blob in (workers_raw or {}).items():
        try:
            info = json.loads(blob)
        except (TypeError, ValueError):
            continue
        workers.append({"id": worker_id, **info})

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
