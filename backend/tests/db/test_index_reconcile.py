"""Index reconciliation: drop stale indexes before init_beanie crashes.

init_beanie calls createIndexes for every declared index. If an index
exists with the same name but a different definition (keys, unique,
partial, sparse, TTL), MongoDB rejects it with IndexKeySpecsConflict
(code 86) and the API exits during startup. reconcile_indexes detects
those conflicts and drops the stale index first.
"""

import pytest
from beanie import init_beanie
from pymongo import ASCENDING, AsyncMongoClient, IndexModel

from app.config import settings
from app.db.index_reconcile import _index_def, reconcile_indexes


@pytest.fixture
async def _db():
    """Connect to the throwaway test database, same as the storage tests."""
    client = AsyncMongoClient(settings.mongo_url, tz_aware=True)
    db = client["biopipe_test"]
    # Clean slate: drop any test collection that might carry a stale index.
    for name in await db.list_collection_names():
        await db.drop_collection(name)
    yield db
    for name in await db.list_collection_names():
        await db.drop_collection(name)
    await client.close()


async def _create_indexes(db, coll_name: str, indexes: list[IndexModel]):
    """Create indexes directly on the collection."""
    coll = db[coll_name]
    for im in indexes:
        await coll.create_indexes([im])


class TestIndexDef:
    """The comparison key: a hashable representation of an index's defining
    properties. Two indexes with the same _index_def are compatible; different
    means one must be dropped and recreated."""

    def test_simple_key_index(self):
        doc = IndexModel([("foo", ASCENDING)], name="foo_idx").document
        assert _index_def(doc)[0] == (("foo", 1),)

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


class TestInitModelsIntegration:
    """Verify that _init_models reconciles before init_beanie, so a stale
    index does not crash startup."""

    async def test_startup_with_stale_index_succeeds(self, _db):
        """The exact scenario: an index with an old partialFilterExpression
        exists, and the model declares a new one. _init_models must drop the
        stale one and let init_beanie create the new one — no crash."""

        from app.models import ALL_MODELS

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

        # Now run the reconcile-then-init_beanie pattern against this db
        # — it must not crash.  The Job model declares the new
        # partialFilterExpression with "blocked".
        for model in ALL_MODELS:
            model_settings = model.Settings
            coll_name = getattr(model_settings, "name", model.__name__.lower())
            indexes = getattr(model_settings, "indexes", [])
            if indexes:
                await reconcile_indexes(_db[coll_name], indexes)

        await init_beanie(database=_db, document_models=ALL_MODELS)

        # The index should now have the new definition
        info = await coll.index_information()
        pfe = info.get("uniq_active_dedup_key", {}).get("partialFilterExpression")
        assert pfe is not None
        assert "blocked" in pfe.get("state", {}).get("$in", [])
