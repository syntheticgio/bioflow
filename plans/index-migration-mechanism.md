# Index Migration Mechanism — Startup-Safe Index Reconciliation

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** A migration mechanism that reconciles Beanie's declared indexes against the live database at startup, detecting and resolving `IndexKeySpecsConflict` (code 86) automatically rather than crashing the API — so the next index definition change does not require a manual `db.jobs.dropIndex(...)` session.

**Architecture:** A `reconcile_indexes` step runs after `init_beanie` in `connect_to_mongo()`. For each model in `ALL_MODELS`, it compares the indexes Beanie declared (`Settings.indexes`) against what MongoDB actually has. When an index name exists in both but with a different definition (keys, unique, partial, sparse, TTL), the stale index is dropped and Beanie recreates it via `createIndexes`. When the database has an index Beanie no longer declares, it is left alone (orphaned indexes are harmless; dropping them is a judgment call this system does not make automatically). The entire reconciliation is logged: every drop, every create, every skip. The mechanism runs on every startup — not just on "migration" events — so it is self-healing: a partially-applied migration from a crashed previous startup completes on the next boot.

**Tech Stack:** Python 3, motor (async MongoDB driver), Beanie ODM, pymongo `IndexModel`, FastAPI startup lifecycle. Tests are pytest (`asyncio_mode = "auto"`), running inside the Docker `api` container against the `biopipe_test` database.

---

## Background for the engineer

**Read the TODO entry first.** This plan implements item 4 in `docs/TODO.md`:

> Changing an index definition is a hard startup failure. `init_beanie` calls
> `createIndexes` with the new `partialFilterExpression` under a name that
> already exists, MongoDB rejects it with `IndexKeySpecsConflict` (code 86), and
> the API exits during startup. Not a quiet inconsistency: the container will
> not boot at all against a database that predates the change.

The specific index that triggered this — `uniq_active_dedup_key` on the `jobs` collection, which gained a `"blocked"` state in its `partialFilterExpression` — has already been fixed manually on this machine's `biopipe` and `biopipe_test` databases. But the project has no general mechanism; the next index change will hit the same wall.

**How Beanie handles indexes today.** `init_beanie` (`db/client.py:55-58`) calls `init_beanie(database=..., document_models=ALL_MODELS)`. Beanie's `init_beanie` iterates each model's `Settings.indexes` list and calls `collection.create_indexes()` on the motor collection. `create_indexes` is idempotent for *identical* indexes — if the index exists with the same name, keys, and options, MongoDB returns success. But if the index exists with the *same name* and *different options* (e.g., a changed `partialFilterExpression`, a changed `unique` flag, or different key fields), MongoDB raises `OperationFailure` with code 86 (`IndexKeySpecsConflict`), and the entire `init_beanie` call fails, crashing the API.

**The fix is not "catch the error and continue."** Swallowing `IndexKeySpecsConflict` would leave the stale index in place — the new definition would not be applied, and the behavior the code depends on (e.g., the dedup guard including `"blocked"`) would silently not hold. The fix is to drop the conflicting index and recreate it with the new definition, *before* `init_beanie` runs, or to retry `init_beanie` after dropping.

**This project has 9 collections with 32 declared indexes** (including `_id_`):

| Collection | Indexes | Has partial/unique? |
|---|---|---|
| `projects` | 4 | `uniq_sibling_name` (unique) |
| `blobs` | 3 | `gc_candidates` (partial), `uniq_external_path` (unique + partial) |
| `objects` | 9 | none |
| `jobs` | 7 | `uniq_active_dedup_key` (unique + partial), `lease_expiry` (partial), `ttl` (TTL) |
| `upload_sessions` | 3 | `by_client_hash` (sparse), `ttl` (TTL) |
| `schedules` | 0 | — |
| `job_timings` | 1 | none |
| `pipeline_runs` | 2 | none |
| `run_jobs` | 3 | `uniq_run_job` (unique) |

The indexes most likely to change in the future are the partial/unique ones — `partialFilterExpression` changes when `JobState` gains a new non-terminal state (the `blocked` addition was the case that triggered this), and unique constraints are occasionally relaxed to partial when the "every null is the same null" problem surfaces. TTL indexes (`expireAfterSeconds`) are also candidates: the retention period is a setting that could change.

**Run tests inside Docker:** `docker compose exec -T api pytest <path> -v`. The stack is up; backend source and tests are bind-mounted from the host, so edits apply immediately.

**No conftest at the tests/ root.** The storage tests (`test_object_role.py`, `test_sidecars.py`) each have their own `init_beanie` fixture scoped to the module, connecting to `biopipe_test`. This plan's tests follow the same pattern — a module-scoped fixture that initializes Beanie against `biopipe_test`, then cleans up.

---

## Design decisions settled before writing

**1. Reconcile before `init_beanie`, not after.**

`init_beanie` is what crashes. So the reconciliation must run *before* `init_beanie`, or `init_beanie` must be wrapped in a retry that drops the conflicting index and retries.

The approach chosen: **reconcile before `init_beanie`**. We read each model's `Settings.indexes`, compare against the live database's `index_information()`, drop any index whose definition differs, and then let `init_beanie` create all indexes fresh. This is cleaner than a retry loop because:

- The comparison is explicit and logged — you can see exactly what changed and what was dropped.
- `init_beanie` runs once, normally, with no conflicts remaining.
- A retry loop would catch the first conflict, drop it, retry, catch the second, drop it, retry... — N round trips for N changed indexes, with less clear logging.

**2. Compare by index name, not by key spec.**

MongoDB indexes are identified by name. Two indexes with the same name but different keys is the conflict case. Two indexes with the same keys but different names is fine (they coexist). So the comparison is: for each index name that appears in both the model's declaration and the database, do the key, unique, sparse, partial, and TTL options match? If not, drop the database's copy and let `init_beanie` recreate it.

**3. Orphaned indexes (in DB but not in model) are left alone.**

If an index exists in the database that no model declares, this system does NOT drop it. Reasons:

- Dropping an index is destructive. An orphaned index might be used by a query the model no longer declares but that still runs somewhere. Dropping it would silently degrade performance.
- The Beanie `Settings.indexes` list is the source of truth for what the *application* declares, but a user or admin might have added a custom index manually.
- The safe default is to log the orphan and let a human decide. The TODO entry is about definition *conflicts*, not about cleaning up old indexes.

**4. `_id_` is always skipped.**

MongoDB creates the `_id_` index automatically and it cannot be dropped. The reconciliation skips it by name.

**5. The mechanism is idempotent and runs every startup.**

There is no "migration version" or "has this migration run" flag. The reconciliation compares declarations against reality on every boot. If nothing changed, it drops nothing and creates nothing — `init_beanie`'s `create_indexes` is a no-op for existing identical indexes. If something changed, it drops and recreates. This means:

- A partially-applied migration (e.g., index dropped but process crashed before `init_beanie` recreated it) completes on the next boot.
- There is no migration state to track, no version collection, no "did we run migration 3 yet" logic.
- The cost is one `index_information()` call per collection per startup — negligible at 9 collections.

**6. Both `biopipe` and `biopipe_test` are covered.**

The test database (`biopipe_test`) also carries indexes, created by the test fixtures. The reconciliation runs in `connect_to_mongo()`, which connects to `settings.mongo_db` (the app database). The test database is handled by the test fixtures themselves, which call `init_beanie` directly — and those fixtures will benefit from the same reconciliation if it is also applied there (Task 4 adds this).

---

## File structure

| File | Change |
|---|---|
| `backend/app/db/index_reconcile.py` | **Create.** The reconciliation logic: compare declared vs. live indexes, drop conflicts. |
| `backend/app/db/client.py` | Call `reconcile_indexes` before `init_beanie` in `_init_models`. |
| `backend/tests/db/test_index_reconcile.py` | **Create.** Unit tests for the reconciliation logic. |

---

## Task 1: Write the index reconciliation module

**Objective:** Create the `reconcile_indexes` function that compares Beanie's declared indexes against the live database and drops conflicting ones.

**Files:**
- Create: `backend/app/db/index_reconcile.py`

**Step 1: Write the failing tests**

Create `backend/tests/db/test_index_reconcile.py`:

```python
"""Index reconciliation: drop stale indexes before init_beanie crashes.

init_beanie calls createIndexes for every declared index. If an index
exists with the same name but a different definition (keys, unique,
partial, sparse, TTL), MongoDB rejects it with IndexKeySpecsConflict
(code 86) and the API exits during startup. reconcile_indexes detects
those conflicts and drops the stale index first.
"""

import pytest
from motor.motor_asyncio import AsyncIOMotorClient
from beanie import init_beanie
from pymongo import ASCENDING, IndexModel

from app.config import settings
from app.db.index_reconcile import reconcile_indexes, _index_def


@pytest.fixture(scope="module", autouse=True)
async def _db():
    """Connect to the throwaway test database, same as the storage tests."""
    client = AsyncIOMotorClient(settings.mongo_url, tz_aware=True)
    db = client["biopipe_test"]
    # Clean slate: drop any test collection that might carry a stale index.
    for name in await db.list_collection_names():
        await db.drop_collection(name)
    yield db
    for name in await db.list_collection_names():
        await db.drop_collection(name)
    client.close()


async def _create_indexes(db, coll_name: str, indexes: list[IndexModel]):
    """Create indexes directly on the motor collection."""
    coll = db[coll_name]
    for im in indexes:
        await coll.create_indexes([im])


class TestIndexDef:
    """The comparison key: a hashable representation of an index's defining
    properties. Two indexes with the same _index_def are compatible; different
    means one must be dropped and recreated."""

    def test_simple_key_index(self):
        doc = IndexModel([("foo", ASCENDING)], name="foo_idx").document
        assert _index_def(doc) == (("foo", 1),)

    def test_compound_key_order_matters(self):
        a = IndexModel([("a", ASCENDING), ("b", ASCENDING)], name="ab").document
        b = IndexModel([("b", ASCENDING), ("a", ASCENDING)], name="ba").document
        assert _index_def(a) != _index_def(b)

    def test_unique_flag_included(self):
        plain = IndexModel([("x", ASCENDING)], name="x").document
        unique = IndexModel([("x", ASCENDING)], name="x", unique=True).document
        assert _index_def(plain) != _index_def(unique)

    def test_partial_filter_included(self):
        pfe = {"state": {"$in": ["pending", "queued"]}}
        without = IndexModel([("x", ASCENDING)], name="x").document
        with_partial = IndexModel(
            [("x", ASCENDING)], name="x", partialFilterExpression=pfe
        ).document
        assert _index_def(without) != _index_def(with_partial)

    def test_sparse_flag_included(self):
        plain = IndexModel([("x", ASCENDING)], name="x").document
        sparse = IndexModel([("x", ASCENDING)], name="x", sparse=True).document
        assert _index_def(plain) != _index_def(sparse)

    def test_ttl_included(self):
        plain = IndexModel([("x", ASCENDING)], name="x").document
        ttl = IndexModel(
            [("x", ASCENDING)], name="x", expireAfterSeconds=0
        ).document
        assert _index_def(plain) != _index_def(ttl)

    def test_same_definition_equal(self):
        a = IndexModel(
            [("x", ASCENDING)], name="x", unique=True,
            partialFilterExpression={"x": {"$type": "string"}}
        ).document
        b = IndexModel(
            [("x", ASCENDING)], name="x", unique=True,
            partialFilterExpression={"x": {"$type": "string"}}
        ).document
        assert _index_def(a) == _index_def(b)


class TestReconcile:
    """Against a real Mongo (test database), exercising drop + recreate."""

    async def test_drops_index_with_changed_partial_filter(self, _db):
        """The exact scenario from TODO #4: the partialFilterExpression changed."""
        coll = _db["test_jobs"]
        # Simulate the old definition (without "blocked")
        old = IndexModel(
            [("dedup_key", ASCENDING)],
            name="uniq_active_dedup_key",
            unique=True,
            partialFilterExpression={
                "dedup_key": {"$type": "string"},
                "state": {"$in": ["pending", "queued", "delayed", "running"]},
            },
        )
        await _create_indexes(_db, "test_jobs", [old])

        # The new definition (with "blocked")
        new = IndexModel(
            [("dedup_key", ASCENDING)],
            name="uniq_active_dedup_key",
            unique=True,
            partialFilterExpression={
                "dedup_key": {"$type": "string"},
                "state": {"$in": ["pending", "queued", "delayed", "blocked", "running"]},
            },
        )

        dropped = await reconcile_indexes(coll, [new])
        assert dropped == ["uniq_active_dedup_key"]

        # Verify the old index is gone and the new one can now be created
        info = await coll.index_information()
        assert "uniq_active_dedup_key" not in info
        await coll.create_indexes([new])
        info = await coll.index_information()
        assert "uniq_active_dedup_key" in info

    async def test_drops_index_with_changed_unique_flag(self, _db):
        coll = _db["test_unique"]
        non_unique = IndexModel([("x", ASCENDING)], name="x_idx")
        await _create_indexes(_db, "test_unique", [non_unique])

        now_unique = IndexModel([("x", ASCENDING)], name="x_idx", unique=True)
        dropped = await reconcile_indexes(coll, [now_unique])
        assert dropped == ["x_idx"]

    async def test_drops_index_with_changed_keys(self, _db):
        coll = _db["test_keys"]
        old = IndexModel([("a", ASCENDING)], name="compound")
        await _create_indexes(_db, "test_keys", [old])

        new = IndexModel([("a", ASCENDING), ("b", ASCENDING)], name="compound")
        dropped = await reconcile_indexes(coll, [new])
        assert dropped == ["compound"]

    async def test_drops_index_with_changed_ttl(self, _db):
        coll = _db["test_ttl"]
        old = IndexModel([("x", ASCENDING)], name="ttl_idx", expireAfterSeconds=3600)
        await _create_indexes(_db, "test_ttl", [old])

        new = IndexModel([("x", ASCENDING)], name="ttl_idx", expireAfterSeconds=0)
        dropped = await reconcile_indexes(coll, [new])
        assert dropped == ["ttl_idx"]

    async def test_does_not_drop_matching_index(self, _db):
        coll = _db["test_match"]
        idx = IndexModel(
            [("x", ASCENDING)], name="x_idx", unique=True,
            partialFilterExpression={"x": {"$type": "string"}},
        )
        await _create_indexes(_db, "test_match", [idx])

        # Same definition — should not be dropped
        dropped = await reconcile_indexes(coll, [idx])
        assert dropped == []

    async def test_does_not_drop_orphaned_index(self, _db):
        """An index in the DB that the model no longer declares is left alone."""
        coll = _db["test_orphan"]
        declared = IndexModel([("x", ASCENDING)], name="x_idx")
        orphan = IndexModel([("y", ASCENDING)], name="y_idx")
        await _create_indexes(_db, "test_orphan", [declared, orphan])

        # Model only declares x_idx; y_idx is orphaned
        dropped = await reconcile_indexes(coll, [declared])
        assert dropped == []
        info = await coll.index_information()
        assert "y_idx" in info  # still there

    async def test_creates_missing_index(self, _db):
        """An index the model declares but the DB doesn't have: not dropped,
        but reported so the caller knows init_beanie will create it."""
        coll = _db["test_missing"]
        idx = IndexModel([("x", ASCENDING)], name="new_idx")
        dropped = await reconcile_indexes(coll, [idx])
        assert dropped == []  # nothing to drop; init_beanie will create it

    async def test_skips_id_index(self, _db):
        coll = _db["test_id"]
        idx = IndexModel([("x", ASCENDING)], name="x_idx")
        await _create_indexes(_db, "test_id", [idx])

        dropped = await reconcile_indexes(coll, [idx])
        # _id_ is never in the declared list, but if it were, it would be skipped
        assert dropped == []

    async def test_multiple_conflicts_dropped_together(self, _db):
        coll = _db["test_multi"]
        old_a = IndexModel([("a", ASCENDING)], name="a_idx")
        old_b = IndexModel([("b", ASCENDING)], name="b_idx", unique=True)
        await _create_indexes(_db, "test_multi", [old_a, old_b])

        new_a = IndexModel([("a", ASCENDING), ("c", ASCENDING)], name="a_idx")
        new_b = IndexModel([("b", ASCENDING)], name="b_idx")  # unique removed
        dropped = await reconcile_indexes(coll, [new_a, new_b])
        assert set(dropped) == {"a_idx", "b_idx"}
```

**Step 2: Run to verify failure**

```bash
docker compose exec -T api pytest tests/db/test_index_reconcile.py -v
```

Expected: FAIL — `ImportError: cannot import name 'reconcile_indexes' from 'app.db.index_reconcile'`

**Step 3: Write the module**

Create `backend/app/db/index_reconcile.py`:

```python
"""Reconcile Beanie's declared indexes against the live database at startup.

init_beanie calls createIndexes for every index in a model's Settings.indexes.
If an index exists with the same name but a different definition — different
keys, unique flag, partialFilterExpression, sparse, or TTL — MongoDB rejects
it with IndexKeySpecsConflict (code 86) and the API exits during startup.

This module runs before init_beanie, compares each declared index against what
the database actually has, and drops any whose definition differs. init_beanie
then recreates them cleanly. The mechanism is idempotent: if nothing changed,
nothing is dropped, and create_indexes is a no-op for identical indexes.

Orphaned indexes (in the DB but not in the model) are left alone. Dropping an
index is destructive; the safe default is to log and let a human decide.
"""

from typing import Any

from motor.motor_asyncio import AsyncIOMotorCollection
from pymongo import IndexModel

from app.logging import get_logger

log = get_logger(__name__)


def _index_def(doc: dict) -> tuple:
    """A hashable representation of the properties that define an index.

    Two indexes with the same _index_def are compatible: createIndexes is a
    no-op. Different means the existing one must be dropped before the new
    one can be created, or MongoDB raises IndexKeySpecsConflict (code 86).

    The properties compared are exactly those MongoDB considers part of the
    index specification: key pattern, unique, partialFilterExpression, sparse,
    and expireAfterSeconds. Collation is not compared because this project
    does not use collated indexes; adding one would require extending this
    function, which is the right place for it.
    """
    # The key pattern: a list of (field, direction) pairs.
    # pymongo returns this as a list of tuples, which is hashable.
    key = tuple(doc.get("key", []))

    flags = (
        doc.get("unique", False),
        doc.get("sparse", False),
    )

    # partialFilterExpression: a dict or absent. Convert to a frozenset of
    # items so it is hashable and order-independent. None and absent are
    # treated the same (no partial filter).
    pfe = doc.get("partialFilterExpression")
    pfe_key = frozenset(pfe.items()) if pfe else None

    # TTL: expireAfterSeconds. None and absent are the same (no TTL).
    ttl = doc.get("expireAfterSeconds")

    return (key, flags, pfe_key, ttl)


async def reconcile_indexes(
    collection: AsyncIOMotorCollection,
    declared: list[IndexModel],
) -> list[str]:
    """Drop indexes whose definition conflicts with what the model declares.

    Returns the list of index names that were dropped. Safe to call on every
    startup: if nothing changed, returns an empty list.

    Orphaned indexes (in the DB but not declared) are NOT dropped — that is a
    destructive operation left to a human.
    """
    live = await collection.index_information()

    # Map declared index names to their definition.
    declared_map: dict[str, dict] = {}
    for im in declared:
        doc = im.document
        name = doc.get("name")
        if name:
            declared_map[name] = doc

    to_drop: list[str] = []

    for name, live_doc in live.items():
        if name == "_id_":
            continue  # cannot be dropped, never conflicts

        if name not in declared_map:
            # Orphaned: in the DB but not declared. Leave it alone.
            log.info(
                "index_orphaned",
                collection=collection.name,
                index=name,
                keys=live_doc.get("key"),
            )
            continue

        live_def = _index_def(live_doc)
        declared_def = _index_def(declared_map[name])

        if live_def != declared_def:
            to_drop.append(name)
            log.info(
                "index_conflict",
                collection=collection.name,
                index=name,
                live=live_def,
                declared=declared_def,
            )

    for name in to_drop:
        await collection.drop_index(name)
        log.info("index_dropped", collection=collection.name, index=name)

    return to_drop
```

**Step 4: Run the tests**

```bash
docker compose exec -T api pytest tests/db/test_index_reconcile.py -v
```

Expected: PASS, all tests green.

**Step 5: Commit**

```bash
git add backend/app/db/index_reconcile.py backend/tests/db/test_index_reconcile.py
git commit -m "feat: add index reconciliation module for startup-safe index changes"
```

---

## Task 2: Wire reconciliation into the startup path

**Objective:** Call `reconcile_indexes` for every model before `init_beanie` runs, so conflicting indexes are dropped before they can crash `init_beanie`.

**Files:**
- Modify: `backend/app/db/client.py:55-58` (the `_init_models` function)

**Step 1: Write the test**

Append to `backend/tests/db/test_index_reconcile.py`:

```python
class TestInitModelsIntegration:
    """Verify that _init_models reconciles before init_beanie, so a stale
    index does not crash startup."""

    async def test_startup_with_stale_index_succeeds(self, _db):
        """The exact scenario: an index with an old partialFilterExpression
        exists, and the model declares a new one. _init_models must drop the
        stale one and let init_beanie create the new one — no crash."""
        from app.db.client import _init_models
        from app.models import ALL_MODELS
        from beanie import Document, PydanticObjectId
        from pymongo import ASCENDING, IndexModel
        import beanie

        # Create a stale index directly on the test DB's jobs collection
        coll = _db["jobs"]
        stale = IndexModel(
            [("dedup_key", ASCENDING)],
            name="uniq_active_dedup_key",
            unique=True,
            partialFilterExpression={
                "dedup_key": {"$type": "string"},
                "state": {"$in": ["pending", "queued", "delayed", "running"]},
            },
        )
        await coll.create_indexes([stale])

        # Now run _init_models against this database — it must not crash
        # The Job model declares the new partialFilterExpression with "blocked"
        client = AsyncIOMotorClient(settings.mongo_url, tz_aware=True)
        await _init_models_with_db(client, _db.name)
        client.close()

        # The index should now have the new definition
        info = await coll.index_information()
        pfe = info.get("uniq_active_dedup_key", {}).get("partialFilterExpression")
        assert pfe is not None
        assert "blocked" in pfe.get("state", {}).get("$in", [])


async def _init_models_with_db(client, db_name):
    """Helper: run the same _init_models logic but against a specific db."""
    from app.db.index_reconcile import reconcile_indexes
    from app.models import ALL_MODELS
    from beanie import init_beanie

    db = client[db_name]
    for model in ALL_MODELS:
        settings = model.Settings
        coll_name = getattr(settings, "name", model.__name__.lower())
        indexes = getattr(settings, "indexes", [])
        if indexes:
            await reconcile_indexes(db[coll_name], indexes)

    await init_beanie(database=db, document_models=ALL_MODELS)
```

**Step 2: Run to verify it fails**

```bash
docker compose exec -T api pytest tests/db/test_index_reconcile.py::TestInitModelsIntegration -v
```

Expected: FAIL — `_init_models` does not yet call `reconcile_indexes`, so `init_beanie` will raise `IndexKeySpecsConflict` against the stale index.

**Step 3: Modify `_init_models`**

In `backend/app/db/client.py`, change `_init_models` from:

```python
async def _init_models(client: AsyncIOMotorClient) -> None:
    from app.models import ALL_MODELS

    await init_beanie(database=client[settings.mongo_db], document_models=ALL_MODELS)
```

to:

```python
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
```

The reconciliation runs for every model that declares indexes, before `init_beanie`. If nothing changed, `reconcile_indexes` returns an empty list and `init_beanie` is a no-op for existing indexes. If something changed, the stale index is dropped and `init_beanie` recreates it.

**Step 4: Run the tests**

```bash
docker compose exec -T api pytest tests/db/test_index_reconcile.py -v
```

Expected: PASS, all tests green.

**Step 5: Run the full test suite to check for regressions**

```bash
docker compose exec -T api pytest -v
```

Expected: all tests pass. The reconciliation runs on every `init_beanie` call now, including in test fixtures that call `init_beanie` directly — but those fixtures do not go through `_init_models`, so they are unaffected. The storage tests that do call `init_beanie` directly against `biopipe_test` may have stale indexes from previous runs; if any test fails with `IndexKeySpecsConflict`, that is the exact bug this feature fixes — the test fixture should also call `reconcile_indexes` (addressed in Task 3).

**Step 6: Commit**

```bash
git add backend/app/db/client.py backend/tests/db/test_index_reconcile.py
git commit -m "feat: reconcile indexes before init_beanie at startup"
```

---

## Task 3: Apply reconciliation in test fixtures

**Objective:** The storage test modules that call `init_beanie` directly (`test_object_role.py`, `test_sidecars.py`) also need reconciliation, or they will hit the same `IndexKeySpecsConflict` when an index definition changes between runs.

**Files:**
- Modify: `backend/tests/storage/test_object_role.py:15-26` (the `_init_beanie_models` fixture)
- Modify: `backend/tests/storage/test_sidecars.py:10-29` (the equivalent fixture)

**Step 1: Update the `test_object_role.py` fixture**

Change the `_init_beanie_models` fixture from:

```python
@pytest.fixture(scope="module", autouse=True)
async def _init_beanie_models():
    client = AsyncIOMotorClient(settings.mongo_url, tz_aware=True)
    await init_beanie(database=client["biopipe_test"], document_models=ALL_MODELS)
    yield
    client.close()
```

to:

```python
@pytest.fixture(scope="module", autouse=True)
async def _init_beanie_models():
    from app.db.index_reconcile import reconcile_indexes

    client = AsyncIOMotorClient(settings.mongo_url, tz_aware=True)
    db = client["biopipe_test"]
    for model in ALL_MODELS:
        model_settings = model.Settings
        coll_name = getattr(model_settings, "name", model.__name__.lower())
        indexes = getattr(model_settings, "indexes", [])
        if indexes:
            await reconcile_indexes(db[coll_name], indexes)
    await init_beanie(database=db, document_models=ALL_MODELS)
    yield
    client.close()
```

**Step 2: Update the `test_sidecars.py` fixture**

Apply the same change to its `init_beanie` fixture (same pattern, same lines).

**Step 3: Run the full test suite**

```bash
docker compose exec -T api pytest -v
```

Expected: all tests pass, including the storage tests that were previously vulnerable to stale indexes.

**Step 4: Commit**

```bash
git add backend/tests/storage/test_object_role.py backend/tests/storage/test_sidecars.py
git commit -m "test: apply index reconciliation in storage test fixtures"
```

---

## Task 4: Verify against a real stale index

**Objective:** Prove the mechanism works end-to-end by simulating the exact TODO scenario: plant a stale index in the live `biopipe` database and restart the API.

**Step 1: Plant a stale index**

```bash
docker compose exec -T mongo mongosh biopipe --eval '
db.jobs.dropIndex("uniq_active_dedup_key");
db.jobs.createIndex(
  { dedup_key: 1 },
  {
    name: "uniq_active_dedup_key",
    unique: true,
    partialFilterExpression: {
      dedup_key: { $type: "string" },
      state: { $in: ["pending", "queued", "delayed", "running"] }
    }
  }
);
print("Planted stale index (missing 'blocked' state):");
printjson(db.jobs.getIndexes().find(i => i.name === "uniq_active_dedup_key"));
'
```

This simulates the pre-migration state: the `uniq_active_dedup_key` index exists without `"blocked"` in its `partialFilterExpression`.

**Step 2: Restart the API**

```bash
docker compose restart api
```

**Step 3: Verify the API started successfully**

```bash
# Wait for readiness
for i in $(seq 1 30); do
  if curl -fsS http://localhost:8000/readyz >/dev/null 2>&1; then echo "READY"; break; fi
  sleep 2
done
```

Expected: `READY` — the API did not crash on the stale index.

**Step 4: Verify the index was reconciled**

```bash
docker compose exec -T mongo mongosh biopipe --eval '
const idx = db.jobs.getIndexes().find(i => i.name === "uniq_active_dedup_key");
printjson(idx.partialFilterExpression);
'
```

Expected: the `partialFilterExpression` now includes `"blocked"` in the `state.$in` array. The reconciliation detected the conflict, dropped the stale index, and `init_beanie` recreated it with the current definition.

**Step 5: Check the logs for the reconciliation**

```bash
docker compose logs api 2>&1 | grep -E "index_conflict|index_dropped" | tail -5
```

Expected: log lines showing the conflict was detected and the stale index was dropped.

**Step 6: Run the full test suite one final time**

```bash
docker compose exec -T api pytest -v
```

Expected: all tests pass.

---

## Edge cases and states

| Scenario | Behavior |
|---|---|
| No indexes changed since last startup | `reconcile_indexes` compares definitions, finds no conflicts, drops nothing. `init_beanie` is a no-op. Cost: one `index_information()` per collection. |
| One index definition changed | The stale index is dropped, `init_beanie` recreates it. Logged. |
| Multiple index definitions changed | Each is detected and dropped in one pass. `init_beanie` recreates all. |
| Process crashed after drop, before `init_beanie` | The index is absent. Next startup: `reconcile_indexes` finds no conflict (index is gone), `init_beanie` creates it. Self-healing. |
| Fresh database (no existing indexes) | `reconcile_indexes` finds no live indexes to conflict with. `init_beanie` creates all. |
| Orphaned index (in DB, not in model) | Logged as `index_orphaned`. NOT dropped. A human decides. |
| `_id_` index | Always skipped by name. Never compared, never dropped. |
| TTL index with changed `expireAfterSeconds` | Detected: `_index_def` includes `expireAfterSeconds`. Dropped and recreated. |
| Index with changed `sparse` flag | Detected: `_index_def` includes `sparse`. Dropped and recreated. |
| Network error during `index_information()` | Exception propagates, `connect_to_mongo` fails, API does not start. This is correct: if we cannot read indexes, we cannot safely reconcile, and starting with a stale index is worse than not starting. |

---

## What this does NOT do

- **Does not drop orphaned indexes.** An index in the DB that no model declares is left alone. Dropping is destructive; a human should decide.
- **Does not track migration versions.** There is no `migrations` collection, no version number, no "has this migration run" check. The reconciliation is stateless and runs every boot.
- **Does not handle collation.** This project does not use collated indexes. If one is added, `_index_def` must be extended to include the collation in the comparison tuple.
- **Does not create missing indexes directly.** `reconcile_indexes` only drops conflicts. Index creation is left to `init_beanie`'s `create_indexes`, which is what already runs and is the correct place for it.
- **Does not add a CLI command.** The reconciliation is automatic at startup. A `make migrate` target or CLI command would be a separate, optional addition if manual control is ever needed.
- **Does not change the `biopipe_test` database's fixture pattern.** The test fixtures that call `init_beanie` directly are updated to also call `reconcile_indexes` (Task 3), but they still use the same `biopipe_test` database. A future improvement could give each test module its own isolated database, but that is out of scope.
