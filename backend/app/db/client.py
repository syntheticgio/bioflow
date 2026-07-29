"""MongoDB connection, Beanie initialization, and the replica-set assertion."""

from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.config import settings
from app.logging import get_logger

log = get_logger(__name__)

_client: AsyncIOMotorClient | None = None


def get_client() -> AsyncIOMotorClient:
    if _client is None:
        raise RuntimeError("Mongo client not initialized; call connect_to_mongo() first")
    return _client


def get_db() -> AsyncIOMotorDatabase:
    return get_client()[settings.mongo_db]


async def connect_to_mongo() -> AsyncIOMotorClient:
    global _client
    if _client is not None:
        return _client
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
    from app.models import ALL_MODELS
    from app.db.index_reconcile import reconcile_indexes

    db = client[settings.mongo_db]
    for model in ALL_MODELS:
        model_settings = model.Settings
        coll_name = getattr(model_settings, "name", model.__name__.lower())
        indexes = getattr(model_settings, "indexes", [])
        if indexes:
            await reconcile_indexes(db[coll_name], indexes)

    await init_beanie(database=db, document_models=ALL_MODELS)


async def close_mongo() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None
        log.info("mongo_closed")


async def ping() -> bool:
    try:
        await get_client().admin.command("ping")
        return True
    except Exception as e:  # noqa: BLE001 - health check reports, never raises
        log.warning("mongo_ping_failed", error=str(e))
        return False
