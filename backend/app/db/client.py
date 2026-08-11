"""MongoDB connection, Beanie initialization, and the replica-set assertion."""

import asyncio

from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.config import settings
from app.logging import get_logger

log = get_logger(__name__)

_client: AsyncIOMotorClient | None = None
# The event loop `connect_to_mongo` ran on -- the one Motor's internals are
# bound to. A `HandlerMode.THREAD` handler runs in a worker-pool thread with no
# loop of its own; calling `asyncio.run()` there spins up a *new* loop, and
# Motor's client raises "attached to a different loop" the moment it touches
# that new loop's futures. `run_coroutine_threadsafe` against this stored loop
# is the fix -- the same pattern `queue/executor.py`'s
# `_schedule_lease_extension` already uses for the identical problem.
_loop: asyncio.AbstractEventLoop | None = None


def get_client() -> AsyncIOMotorClient:
    if _client is None:
        raise RuntimeError("Mongo client not initialized; call connect_to_mongo() first")
    return _client


def get_db() -> AsyncIOMotorDatabase:
    return get_client()[settings.mongo_db]


def run_from_thread(coro):
    """Run a Mongo-touching coroutine from a worker thread, blocking it.

    For `HandlerMode.THREAD` handlers, which have no event loop of their own.
    `asyncio.run(coro)` looks like the obvious escape but is wrong here: it
    creates a fresh loop, and this process's Mongo client is bound to the loop
    `connect_to_mongo` ran on, not to whatever loop happens to exist on this
    thread. Scheduling the coroutine onto the real loop and blocking this
    thread for the result is what actually works.
    """
    if _loop is None:
        raise RuntimeError("Mongo client not initialized; call connect_to_mongo() first")
    return asyncio.run_coroutine_threadsafe(coro, _loop).result()


async def connect_to_mongo() -> AsyncIOMotorClient:
    global _client, _loop
    if _client is not None:
        return _client
    _loop = asyncio.get_running_loop()
    _client = AsyncIOMotorClient(settings.mongo_url, tz_aware=True)
    await _assert_replica_set(_client)
    await _init_models(_client)
    log.info("mongo_connected", db=settings.mongo_db)
    return _client


async def _assert_replica_set(client: AsyncIOMotorClient) -> None:
    """Refuse to start against a standalone mongod.

    Without a replica set, multi-document transactions do not merely fail --
    pymongo raises only when one is attempted, and any code path that forgets
    to use one degrades silently. The CAS refcounting path depends on
    transactional consistency between `objects` and `blobs`, so this is checked
    once, loudly, at startup.
    """
    hello = await client.admin.command("hello")
    set_name = hello.get("setName")
    if not set_name:
        raise RuntimeError(
            "MongoDB is running as a standalone, but this application requires a "
            "replica set for multi-document transactions. Start mongod with "
            "--replSet rs0 and run rs.initiate(). See docker-compose.yml."
        )
    log.info("mongo_replica_set_ok", set_name=set_name)


async def _init_models(client: AsyncIOMotorClient) -> None:
    from app.db.index_reconcile import reconcile_indexes
    from app.models import ALL_MODELS

    db = client[settings.mongo_db]
    for model in ALL_MODELS:
        model_settings = model.Settings
        coll_name = getattr(model_settings, "name", model.__name__.lower())
        indexes = getattr(model_settings, "indexes", [])
        if indexes:
            await reconcile_indexes(db[coll_name], indexes)

    await init_beanie(database=db, document_models=ALL_MODELS)


async def close_mongo() -> None:
    global _client, _loop
    if _client is not None:
        _client.close()
        _client = None
        _loop = None
        log.info("mongo_closed")


async def ping() -> bool:
    try:
        await get_client().admin.command("ping")
        return True
    except Exception as e:  # noqa: BLE001 - health check reports, never raises
        log.warning("mongo_ping_failed", error=str(e))
        return False
