"""verify_files' guard 3: a whole-batch miss looks like a mount problem.

Guard 1 (the sentinel check) only catches a *fully* empty mount. A drive that
partially remounted, or a share that serves an empty listing for some paths,
passes guard 1 and would still land most of a batch in the "confirmed
missing" path over a couple of sweeps. This is exactly the shape of the
2026-08-05 incident recorded in docs/TODO.md: 45 blobs went to state=missing
at once, in a combination the two-strike guard cannot itself produce, which
pointed at an out-of-band write -- but the gap it exposed (no protection
against a mass miss) is real regardless of what actually wrote it, so guard 3
closes that gap directly: count misses across the whole batch before writing
anything, and abort with no writes if the rate looks like a mount event
rather than N independent deletions.
"""

from pathlib import Path

import pytest
from beanie import init_beanie
from pymongo import AsyncMongoClient

from app.config import settings
from app.models import ALL_MODELS, Blob, BlobState, BlobStorage
from app.queue import handlers
from app.queue.registry import JobContext
from tests._mongo_isolation import direct_mongo_url, worker_db_name

# No `pytestmark = pytest.mark.asyncio` needed: pyproject.toml sets
# `asyncio_mode = "auto"`.


@pytest.fixture(autouse=True)
async def _init_beanie_models():
    """Function-scoped, not the shared `beanie_models` fixture in
    tests/conftest.py (`scope="module", loop_scope="module"`): pytest-asyncio
    hands each async test its own event loop by default, and mixing a
    module-scoped Motor client with per-test cleanup queries binds it to the
    wrong loop -- same pattern as tests/queue/test_record_outcomes.py.

    Also drops `blobs` on entry: `Blob.find` in verify_files has no owner
    scope (blobs are global), so leftover rows from an earlier test in this
    file would silently inflate the batch under test.
    """
    client = AsyncMongoClient(direct_mongo_url(settings.mongo_url), tz_aware=True)
    db = client[worker_db_name()]
    await db[Blob.Settings.name].drop()
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


async def _make_blob(digest: str, *, ref_count: int = 1) -> Blob:
    blob = Blob(
        id=digest,
        size=10,
        state=BlobState.PRESENT,
        storage=BlobStorage.MANAGED,
        ref_count=ref_count,
    )
    await blob.insert()
    return blob


@pytest.fixture(autouse=True)
def _stub_side_effects(monkeypatch, tmp_path):
    """Keep the sentinel guard open and the event publish a no-op.

    `check_home` and `queue.publish_event` are imported inline inside
    `verify_files`, so they must be patched on the modules that own them, not
    on `handlers` itself.
    """
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


class TestMassMissCircuitBreaker:
    async def test_all_missing_above_the_floor_aborts_with_no_writes(
        self, monkeypatch, _stub_side_effects
    ):
        """20 blobs, all absent -- well past both the floor and the fraction."""
        digests = [f"{i:064d}" for i in range(20)]
        for d in digests:
            await _make_blob(d)

        monkeypatch.setattr(handlers, "_stat_or_none", lambda path: None)

        result = await handlers.verify_files(make_ctx())

        assert result["skipped"] is True
        assert result["reason"] == "mass_miss_circuit_breaker"
        assert result["missed"] == 20

        for d in digests:
            blob = await Blob.get(d)
            assert blob.state is BlobState.PRESENT
            assert blob.miss_count == 0
            assert blob.last_miss_at is None

        assert not any(evt == "blob.missing" for evt, _, _ in _stub_side_effects)
        assert any(evt == "storage.mass_miss" for evt, _, _ in _stub_side_effects)

    async def test_a_few_genuine_misses_below_the_floor_are_not_a_breaker_event(
        self, monkeypatch, _stub_side_effects
    ):
        """A small library where every file really is gone must not trip the
        breaker just because the fraction is 100% -- see _BREAKER_MIN_MISSES."""
        digests = [f"{i:064d}" for i in range(3)]
        for d in digests:
            await _make_blob(d)

        monkeypatch.setattr(handlers, "_stat_or_none", lambda path: None)

        result = await handlers.verify_files(make_ctx())

        assert "reason" not in result
        assert result["checked"] == 3
        assert result["first_miss"] == 3

        for d in digests:
            blob = await Blob.get(d)
            assert blob.miss_count == 1

    async def test_mixed_batch_below_the_fraction_proceeds_normally(
        self, monkeypatch, _stub_side_effects
    ):
        """A handful of real deletions among many present files must not trip
        the breaker -- only a batch that looks like a mount event should."""
        present_digests = [f"1{i:063d}" for i in range(20)]
        missing_digests = [f"2{i:063d}" for i in range(3)]
        for d in present_digests:
            await _make_blob(d)
        for d in missing_digests:
            await _make_blob(d)

        present_set = set(present_digests)

        def _stat_by_name(path: Path):
            name = path.name
            if name in present_set:

                class _Stat:
                    st_size = 10
                    st_mtime = 0.0

                return _Stat()
            return None

        monkeypatch.setattr(handlers, "_stat_or_none", _stat_by_name)

        result = await handlers.verify_files(make_ctx())

        assert "reason" not in result
        assert result["checked"] == 23
        assert result["present"] == 20
        assert result["first_miss"] == 3

        for d in missing_digests:
            blob = await Blob.get(d)
            assert blob.miss_count == 1
        for d in present_digests:
            blob = await Blob.get(d)
            assert blob.state is BlobState.PRESENT
            assert blob.miss_count == 0
