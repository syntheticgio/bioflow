"""Shared test fixtures.

`beanie_models` is here rather than copy-pasted into each test module because
three files now need it. It targets a throwaway `biopipe_test` database, so it
never touches real data.
"""

import importlib

import pytest
import pytest_asyncio
from app.config import settings
from app.models import ALL_MODELS
from beanie import init_beanie
from pymongo import AsyncMongoClient


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def beanie_models():
    """Initialize Beanie against a throwaway database.

    Beanie refuses to instantiate a Document before init_beanie, even for an
    object that is never saved. Requested explicitly rather than autouse: it
    needs a running Mongo, and most tests in this suite are pure-function
    assertions that should not be dragged behind a database dependency they
    do not have.

    Collections are dropped on entry, not exit, so a failed run leaves its data
    behind for inspection.

    Also patches `get_db`/`get_client` to this same connection --
    `blob_service.detach_blob_from_object` (reached transitively through
    `object_service.delete_object`) uses that second, separately-initialized
    handle for a raw Mongo update *and* a multi-document transaction
    (`get_client().start_session()`), rather than going through Beanie, same
    idea as `tests/queue/test_cancel_cleanup.py` (which only needed `get_db`).
    Without both patches, any path touching a blob's refcount fails with
    "Mongo client not initialized" even though Beanie itself is set up and
    working. Reusing this fixture's own client rather than a second one keeps
    the transaction on the same replica-set connection as the writes it needs
    to see.

    Patched in three places, not just `app.db.client`: `blob_service`,
    `project_service`, and `upload_service` each do `from app.db.client import
    get_db(, get_client)` at module level, which binds their own local name at
    first import -- patching only `app.db.client`'s attribute leaves an
    already-imported module's local binding untouched (this bit the first
    version of this fixture, which passed in isolation, by import-order luck,
    but failed once the full suite's collection order changed which module
    imported first).
    """
    from app.db.index_reconcile import reconcile_indexes

    client = AsyncMongoClient(settings.mongo_url, tz_aware=True)
    db = client["biopipe_test"]

    for model in ALL_MODELS:
        model_settings = model.Settings
        coll_name = getattr(model_settings, "name", model.__name__.lower())
        await db[coll_name].drop()
        indexes = getattr(model_settings, "indexes", [])
        if indexes:
            await reconcile_indexes(db[coll_name], indexes)

    await init_beanie(database=db, document_models=ALL_MODELS)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.db.client.get_db", lambda: db)
        mp.setattr("app.db.client.get_client", lambda: client)
        for module_name in (
            "app.services.blob_service",
            "app.services.project_service",
            "app.services.upload_service",
            # search_service joined this list when its facet, metadata-value
            # and bulk-write paths first got tests that hit a real database.
            # It aggregates and updates through raw Motor rather than Beanie,
            # so without the patch those queries ran against the *real*
            # `mongo_db` while the fixtures wrote to `biopipe_test`. That does
            # not look like a missing patch from the test: it reads as an empty
            # facet list and a bulk edit that 404s on the caller's own rows.
            "app.services.search_service",
            # share_service opens its own `get_client().start_session()` for
            # the accept-cascade transaction, same reason blob_service is here.
            "app.services.share_service",
            # executor._write_progress writes progress ticks through raw
            # Motor rather than Beanie. Unpatched, any test that drives a real
            # progress write through JobExecutor.run() silently updates the
            # actual `mongo_db` database instead of `biopipe_test` -- the
            # write succeeds, publishes no error, and the test's own read
            # (which does go through Beanie, hence `biopipe_test`) simply
            # never sees it. Found via test_executor_live_resources.py's
            # sampler-driven progress ticks, which are the first tests to
            # exercise this write path for real.
            "app.queue.executor",
        ):
            module = importlib.import_module(module_name)
            if hasattr(module, "get_db"):
                mp.setattr(module, "get_db", lambda: db)
            if hasattr(module, "get_client"):
                mp.setattr(module, "get_client", lambda: client)
        yield

    await client.close()
