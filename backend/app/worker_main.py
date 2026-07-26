"""Worker process entrypoint."""

import asyncio
import signal

from app.config import settings
from app.db.client import close_mongo, connect_to_mongo
from app.db.redis_client import close_redis, connect_to_redis
from app.errors import StorageUnavailableError
from app.logging import configure_logging, get_logger
from app.queue.worker import Worker
from app.storage.home import initialize_home

log = get_logger(__name__)


async def main() -> None:
    configure_logging()

    try:
        initialize_home()
    except StorageUnavailableError as e:
        # Keep running: many jobs do not touch storage, and a worker that exits
        # here would just crash-loop while the drive is remounted.
        log.error("storage_unavailable_at_startup", detail=e.message)

    await connect_to_mongo()
    await connect_to_redis()

    # Idempotent: only creates schedules that do not exist, so user edits to
    # intervals survive a restart.
    from app.queue import scheduler

    await scheduler.seed_defaults()

    worker = Worker()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.request_shutdown)

    try:
        await worker.start()
    finally:
        await close_redis()
        await close_mongo()


if __name__ == "__main__":
    log.info("worker_boot", concurrency=settings.worker_max_concurrent)
    asyncio.run(main())
