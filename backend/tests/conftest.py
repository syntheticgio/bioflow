"""Shared test fixtures.

`beanie_models` is here rather than copy-pasted into each test module because
three files now need it. It targets a throwaway `biopipe_test` database, so it
never touches real data.
"""

import pytest
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from app.config import settings
from app.models import ALL_MODELS


@pytest.fixture(scope="module")
async def beanie_models():
    """Initialize Beanie against a throwaway database.

    Beanie refuses to instantiate a Document before init_beanie, even for an
    object that is never saved. Requested explicitly rather than autouse: it
    needs a running Mongo, and most tests in this suite are pure-function
    assertions that should not be dragged behind a database dependency they
    do not have.

    Collections are dropped on entry, not exit, so a failed run leaves its data
    behind for inspection.
    """
    from app.db.index_reconcile import reconcile_indexes

    client = AsyncIOMotorClient(settings.mongo_url, tz_aware=True)
    db = client["biopipe_test"]

    for model in ALL_MODELS:
        model_settings = model.Settings
        coll_name = getattr(model_settings, "name", model.__name__.lower())
        await db[coll_name].drop()
        indexes = getattr(model_settings, "indexes", [])
        if indexes:
            await reconcile_indexes(db[coll_name], indexes)

    await init_beanie(database=db, document_models=ALL_MODELS)
    yield
    client.close()
