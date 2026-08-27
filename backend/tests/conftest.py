"""Shared test fixtures.

`beanie_models` is here rather than copy-pasted into each test module because
three files now need it. It targets a throwaway, per-run/per-worker database
(`biopipe_test_{token}_{worker}`), so it never touches real data.
"""

import importlib
import os
import sys
import time

import pytest
import pytest_asyncio
from beanie import init_beanie
from hypothesis import settings as hypothesis_settings
from pymongo import AsyncMongoClient, MongoClient

from app.config import settings
from app.models import ALL_MODELS
from tests._mongo_isolation import (
    direct_mongo_url,
    ensure_run_token,
    run_prefix,
    stale_test_dbs,
    worker_db_name,
)

hypothesis_settings.register_profile("dev", max_examples=10)
hypothesis_settings.register_profile("ci", max_examples=100)
hypothesis_settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "dev"))


def pytest_configure(config):
    # Mint the run token on the controller; xdist workers inherit it via env.
    ensure_run_token()


def pytest_report_header(config):
    # Where a failed run's data will be, for whoever inspects it.
    return f"biopipe test databases: {run_prefix()}*"


def _is_controller(config) -> bool:
    # xdist workers carry workerinput; the controller (and non-xdist runs) do not.
    return not hasattr(config, "workerinput")


def _sync_mongo_client() -> MongoClient:
    from app.config import settings as _settings

    return MongoClient(
        direct_mongo_url(_settings.mongo_url),
        tz_aware=True,
        serverSelectionTimeoutMS=2000,
    )


def pytest_sessionstart(session):
    """Sweep run databases abandoned by failed sessions older than 2h."""
    if not _is_controller(session.config):
        return
    try:
        client = _sync_mongo_client()
        names = client.list_database_names()
        metas = {}
        for name in names:
            if name.startswith("biopipe_test_"):
                doc = client[name]["_run_meta"].find_one()
                metas[name] = doc.get("started_at") if doc else None
        for name in stale_test_dbs(names, metas, time.time(), run_prefix()):
            client.drop_database(name)
        client.close()
    except Exception as exc:  # pure-function runs may have no Mongo at all
        print(f"biopipe test-db sweep skipped: {exc}", file=sys.stderr)


def pytest_sessionfinish(session, exitstatus):
    """Drop this run's databases unless something failed worth inspecting.

    Keyed on this run's own failures rather than on `exitstatus == 0`, which
    was the first rule and was wrong in the case that matters: a suite with
    any long-standing red test never cleans up at all, so every run leaks
    one database per worker forever. Measured at 51 leaked databases across
    a dozen runs while three unrelated tests were failing on main.

    `BIOPIPE_KEEP_TEST_DBS=1` keeps them regardless, for a failure that
    needs the data after the run has already exited.
    """
    if not _is_controller(session.config):
        return
    if os.getenv("BIOPIPE_KEEP_TEST_DBS"):
        return
    if session.testsfailed:
        print(
            f"biopipe: keeping {run_prefix()}* for inspection "
            f"({session.testsfailed} failed); BIOPIPE_KEEP_TEST_DBS=1 to always keep",
            file=sys.stderr,
        )
        return
    try:
        client = _sync_mongo_client()
        for name in client.list_database_names():
            if name.startswith(run_prefix()):
                client.drop_database(name)
        client.close()
    except Exception as exc:
        print(f"biopipe test-db cleanup skipped: {exc}", file=sys.stderr)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def beanie_models():
    """Initialize Beanie against a throwaway database.

    The database is per-run and per-worker -- `biopipe_test_{token}_{worker}`
    -- because two concurrent pytest sessions (two agents, two worktrees)
    and two xdist workers within one session all share the same Mongo, and a
    single shared name means each truncates the others' data mid-test.

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
    client = AsyncMongoClient(direct_mongo_url(settings.mongo_url), tz_aware=True)
    db = client[worker_db_name()]

    # Claim the database before touching anything else in it. This write is
    # what creates it, and until the marker lands a concurrent run's sweep
    # cannot tell a database being born from one abandoned by a crash.
    await db["_run_meta"].replace_one({}, {"started_at": time.time()}, upsert=True)

    for coll_name in await db.list_collection_names():
        if not coll_name.startswith("system.") and coll_name != "_run_meta":
            await db[coll_name].delete_many({})

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
            # `mongo_db` while the fixtures wrote to this run's own test
            # database. That does not look like a missing patch from the test:
            # it reads as an empty facet list and a bulk edit that 404s on the
            # caller's own rows.
            "app.services.search_service",
            # share_service opens its own `get_client().start_session()` for
            # the accept-cascade transaction, same reason blob_service is here.
            "app.services.share_service",
            # executor._write_progress writes progress ticks through raw
            # Motor rather than Beanie. Unpatched, any test that drives a real
            # progress write through JobExecutor.run() silently updates the
            # actual `mongo_db` database instead of this run's own -- the
            # write succeeds, publishes no error, and the test's own read
            # (which does go through Beanie, hence the run database) simply
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



@pytest.fixture(autouse=True)
def _register_roots_cover_tmp_path(request, monkeypatch):
    """Let `tmp_path` count as an allowed root for job-payload input paths.

    Handlers resolve `*_path` payload values through
    `storage.paths.resolve_job_input_path`, which refuses anything outside the
    register roots and BioFlow's own directories (#873). Tests build their
    inputs under `tmp_path`, which is neither, so without this every handler
    test that passes a path would fail on containment rather than on the thing
    it is testing.

    Deliberately widens the allowlist rather than disabling the check: the
    resolution, the symlink handling and the refusal all still run in every one
    of these tests, against a root that happens to be the test's own directory.
    A test that wants to prove the *refusal* names a path outside `tmp_path`.
    """
    if "tmp_path" not in request.fixturenames:
        return
    tmp_path = request.getfixturevalue("tmp_path")
    existing = settings.bioinfo_register_roots
    monkeypatch.setattr(
        settings, "bioinfo_register_roots", f"{existing}:{tmp_path}"
    )
