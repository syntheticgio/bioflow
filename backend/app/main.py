"""FastAPI application factory and lifespan."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1 import api_router
from app.api.v1.health import router as health_router
from app.config import settings
from app.db.client import close_mongo, connect_to_mongo
from app.db.redis_client import close_redis, connect_to_redis, get_redis
from app.errors import StorageUnavailableError, register_exception_handlers
from app.logging import configure_logging, get_logger
from app.pipelines import tool_cache
from app.queue.registry import load_handlers
from app.storage.home import initialize_home

log = get_logger(__name__)


async def _warm_tools() -> None:
    """Probe every tool in the background, so a user does not pay for it.

    Never awaited by `lifespan` and deliberately not gating `/readyz`: a
    container that reports unready while probing is a worse experience than the
    stall this removes, and a probe that fails should not keep the app from
    serving. Exceptions are caught here rather than left to surface at
    garbage-collection time as "task exception was never retrieved".
    """
    try:
        await tool_cache.warm(get_redis())
    except Exception as e:  # noqa: BLE001 - a warm failure is never fatal
        log.warning("tool_warm_failed", error=str(e))


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    log.info("starting", home=str(settings.bioinfo_home))

    # Storage first: if the drive is missing, say so plainly rather than
    # failing later inside a request with a confusing error.
    try:
        initialize_home()
    except StorageUnavailableError as e:
        # Start anyway so /readyz can explain the problem. A container that
        # refuses to boot gives the user no diagnostic surface at all.
        log.error("storage_unavailable_at_startup", detail=e.message)

    await connect_to_mongo()
    await connect_to_redis()

    # Registers handlers so /jobs/types and enqueue validation know what exists.
    # The API never executes jobs; workers do.
    load_handlers()

    # Fire-and-forget: fifteen `<tool> --version` spawns, ~15s cold, of which
    # NanoPlot is 12s. Held in a local so the task is not garbage-collected
    # mid-flight, which would cancel it silently.
    warm_task = asyncio.create_task(_warm_tools())

    log.info("started")
    try:
        yield
    finally:
        warm_task.cancel()
        await close_redis()
        await close_mongo()
        log.info("stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="BioFlow",
        description="Local bioinformatics data manager",
        version="0.1.0",
        lifespan=lifespan,
    )
    register_exception_handlers(app)
    app.include_router(health_router)
    app.include_router(api_router)
    return app


app = create_app()
