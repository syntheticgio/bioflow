"""FastAPI application factory and lifespan."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from app.api.deps import target_node_ctx
from app.api.v1 import api_router
from app.api.v1.health import router as health_router
from app.config import settings
from app.db.client import close_mongo, connect_to_mongo
from app.db.redis_client import close_redis, connect_to_redis, get_redis
from app.errors import StorageUnavailableError, register_exception_handlers
from app.logging import configure_logging, get_logger
from app.mcp.server import mount_mcp_app
from app.metadata.platform_migration import split_platform_from_instrument_model
from app.pipelines import tool_cache
from app.queue.registry import load_handlers
from app.services.agent_service import agent_service
from app.services.git_revision import log_revision
from app.storage.home import initialize_home
from app.version import __version__

log = get_logger(__name__)

# How often the lifespan sweeps for idle agent processes.
_AGENT_CLEANUP_INTERVAL = 60


async def _agent_idle_cleanup() -> None:
    """Reap agent subprocesses that have been silent past the idle timeout.

    Agent processes are children of this api process, so they live and die
    with it; this sweep is what keeps a forgotten drawer from holding a pi
    subprocess open for weeks. A no-op while nothing is idle.
    """
    while True:
        await asyncio.sleep(_AGENT_CLEANUP_INTERVAL)
        await agent_service.cleanup_idle()


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

    # Before anything else that could fail confusingly: say which checkout is
    # being served. A stale one makes every merged fix it predates look
    # missing, and the resulting error always blames some other subsystem.
    log_revision()

    # Storage first: if the drive is missing, say so plainly rather than
    # failing later inside a request with a confusing error.
    try:
        initialize_home()
    except StorageUnavailableError as e:
        # Start anyway so /readyz can explain the problem. A container that
        # refuses to boot gives the user no diagnostic surface at all.
        log.error("storage_unavailable_at_startup", detail=e.message)

    await connect_to_mongo()

    from app.services.ai.migration import seed_legacy_provider

    await seed_legacy_provider()

    # #525 split `metadata.platform` into a closed platform tag and an open
    # `instrument_model`. Carries existing rows across; a no-op once done.
    await split_platform_from_instrument_model()

    await connect_to_redis()

    # Registers handlers so /jobs/types and enqueue validation know what exists.
    # The API never executes jobs; workers do.
    load_handlers()

    # Fire-and-forget: fifteen `<tool> --version` spawns, ~15s cold, of which
    # NanoPlot is 12s. Held in a local so the task is not garbage-collected
    # mid-flight, which would cancel it silently.
    warm_task = asyncio.create_task(_warm_tools())

    # Runs for the process lifetime: an install or uninstall of an
    # ON_DEMAND_IMAGE tool (task 4) publishes on this channel from whichever
    # process performed it, and this is what lets *this* process's probe
    # cache learn about it instead of serving a stale result until it happens
    # to restart. See the comment on tool_cache.NOT_FINGERPRINTABLE for why
    # the fingerprint-based invalidation everything else here relies on
    # cannot reach these tools.
    invalidation_task = asyncio.create_task(tool_cache.listen_for_invalidations(get_redis()))

    # The drawer's agent processes: reap the idle ones on a schedule.
    agent_cleanup_task = asyncio.create_task(_agent_idle_cleanup())

    log.info("started")
    try:
        yield
    finally:
        agent_cleanup_task.cancel()
        # Kill any agent subprocess still running; children of this process,
        # they get no second chance after the api dies.
        await agent_service.shutdown_all()
        warm_task.cancel()
        invalidation_task.cancel()
        await close_redis()
        await close_mongo()
        log.info("stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="BioFlow",
        description="Local bioinformatics data manager",
        version=__version__,
        lifespan=lifespan,
    )
    register_exception_handlers(app)

    @app.middleware("http")
    async def extract_target_node(request: Request, call_next):
        """Capture ?target_node= from pipeline launch URLs so enqueue() can route."""
        tn = request.query_params.get("target_node")
        token = target_node_ctx.set(tn or None)
        try:
            return await call_next(request)
        finally:
            target_node_ctx.reset(token)

    app.include_router(health_router)
    app.include_router(api_router)
    # Chains the MCP app's own lifespan into this app's -- see
    # mount_mcp_app's docstring for why a plain app.mount() is not enough.
    mount_mcp_app(app)

    # Register orphan provisioning cleanup on startup.
    from app.api.v1.nodes import _clean_orphaned_provisions

    @app.on_event("startup")
    async def _on_startup():
        await _clean_orphaned_provisions()

    return app


app = create_app()
