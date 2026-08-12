"""verify_files' recheck pass: MISSING blobs get a way back.

The main batch excludes MISSING blobs entirely (`Blob.find(Blob.state !=
BlobState.MISSING)`), so without a second pass a blob that lands in MISSING --
whatever the cause -- stays there forever; only a manual repair or a fresh
write through attach_blob_to_object heals it. See docs/TODO.md for the
2026-08-05 incident this gap came out of: 45 blobs got wrongly marked missing
at once and had no automatic way back.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from beanie import PydanticObjectId, init_beanie
from pymongo import AsyncMongoClient

from app.config import settings
from app.models import ALL_MODELS, Blob, BlobState, BlobStorage, DataObject, ObjectStatus
from app.queue import handlers
from app.queue.registry import JobContext

# No `pytestmark = pytest.mark.asyncio` needed: pyproject.toml sets
# `asyncio_mode = "auto"`.


@pytest.fixture(autouse=True)
async def _init_beanie_models():
    """Function-scoped for the same reason as test_verify_files_circuit_breaker.py:
    a module-scoped Motor client mixed with per-test cleanup binds to the wrong
    event loop under pytest-asyncio's per-test loops.
    """
    client = AsyncMongoClient(settings.mongo_url, tz_aware=True)
    db = client["biopipe_test"]
    await db[Blob.Settings.name].drop()
    await db[DataObject.Settings.name].drop()
    await init_beanie(database=db, document_models=ALL_MODELS)
    yield
    await client.close()


def make_ctx(**kw) -> JobContext:
    return JobContext(
        job_id=kw.pop("job_id", "job-1"),
        payload=kw.pop("payload", {}),
        epoch=1,
        attempts=0,
        owner="local",
        **kw,
    )


async def _missing_blob(digest: str, *, miss_count: int = 2) -> Blob:
    now = datetime.now(UTC)
    blob = Blob(
        id=digest,
        size=10,
        state=BlobState.MISSING,
        storage=BlobStorage.MANAGED,
        ref_count=1,
        miss_count=miss_count,
        last_miss_at=now,
        last_verified_at=now - timedelta(hours=1),
    )
    await blob.insert()
    return blob


@pytest.fixture(autouse=True)
def _stub_side_effects(monkeypatch, tmp_path):
    from app.queue import queue as queue_mod
    from app.storage import home as home_mod

    monkeypatch.setattr(
        home_mod, "check_home", lambda: home_mod.HomeStatus(True, "ok", str(tmp_path))
    )

    published = []

    async def _capture(event_type, data, *, owner):
        published.append((event_type, data, owner))

    monkeypatch.setattr(queue_mod, "publish_event", _capture)
    return published


class TestRecheckMissingBlobs:
    async def test_a_missing_blob_whose_file_is_actually_present_is_healed(
        self, monkeypatch, _stub_side_effects
    ):
        digest = "a" * 64
        await _missing_blob(digest)
        obj = DataObject(project_id=PydanticObjectId(), name="f.fa", owner="local", blob_sha256=digest, status=ObjectStatus.MISSING)
        await obj.insert()

        monkeypatch.setattr(handlers, "_stat_or_none", lambda path: object())

        result = await handlers.verify_files(make_ctx())

        assert result["rechecked_missing"] == 1
        assert result["recovered"] == 1

        blob = await Blob.get(digest)
        assert blob.state is BlobState.PRESENT
        assert blob.miss_count == 0
        assert blob.last_miss_at is None

        refreshed = await DataObject.get(obj.id)
        assert refreshed.status is ObjectStatus.READY

        assert any(evt == "blob.recovered" for evt, _, _ in _stub_side_effects)

    async def test_a_still_missing_blob_is_left_alone(self, monkeypatch, _stub_side_effects):
        digest = "b" * 64
        blob = await _missing_blob(digest)
        before_verified_at = blob.last_verified_at

        monkeypatch.setattr(handlers, "_stat_or_none", lambda path: None)

        result = await handlers.verify_files(make_ctx())

        assert result["rechecked_missing"] == 1
        assert result["recovered"] == 0

        refreshed = await Blob.get(digest)
        assert refreshed.state is BlobState.MISSING
        assert refreshed.miss_count == 2
        # Left untouched, so this blob keeps surfacing first in the next
        # recheck rotation rather than being pushed to the back. Compared with
        # a tolerance: Mongo rounds datetimes to millisecond precision, so an
        # exact equality against the Python-side value is not guaranteed.
        assert abs((refreshed.last_verified_at - before_verified_at).total_seconds()) < 0.01

        assert not any(evt == "blob.recovered" for evt, _, _ in _stub_side_effects)

    async def test_recovery_does_not_resurrect_an_object_that_moved_to_error(
        self, monkeypatch, _stub_side_effects
    ):
        """An object could leave MISSING for an unrelated reason (e.g. ERROR)
        before its blob is ever rechecked; healing the blob must not paper
        over that by force-setting every referencing object back to READY."""
        digest = "c" * 64
        await _missing_blob(digest)
        obj = DataObject(project_id=PydanticObjectId(), name="f.fa", owner="local", blob_sha256=digest, status=ObjectStatus.ERROR)
        await obj.insert()

        monkeypatch.setattr(handlers, "_stat_or_none", lambda path: object())

        await handlers.verify_files(make_ctx())

        refreshed = await DataObject.get(obj.id)
        assert refreshed.status is ObjectStatus.ERROR

    async def test_recheck_batch_size_limits_how_many_missing_blobs_are_tried(
        self, monkeypatch, _stub_side_effects
    ):
        digests = [f"{i:064d}" for i in range(5)]
        for d in digests:
            await _missing_blob(d)

        monkeypatch.setattr(handlers, "_stat_or_none", lambda path: object())

        result = await handlers.verify_files(make_ctx(payload={"recheck_batch_size": 2}))

        assert result["rechecked_missing"] == 2
        assert result["recovered"] == 2

        refreshed_blobs = [await Blob.get(d) for d in digests]
        present_count = sum(1 for b in refreshed_blobs if b.state is BlobState.PRESENT)
        assert present_count == 2
