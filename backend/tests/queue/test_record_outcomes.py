"""Failed runs are recorded for provenance and excluded from every fit.

The bug this guards against is silent and points the wrong way: an OOM kill at
ninety seconds reads as a fast, cheap run whose peak RSS is the ceiling it hit
rather than what it needed. A few in a fit drag estimates down -- toward
causing the next OOM.
"""

import pytest
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from app.config import settings
from app.models import ALL_MODELS
from app.models.timing import JobRunTiming, RunOutcome
from app.services import timing_service

# No `pytestmark = pytest.mark.asyncio` needed: pyproject.toml sets
# `asyncio_mode = "auto"`, so bare `async def` tests are collected.


@pytest.fixture(autouse=True)
async def _init_beanie_models():
    """These tests perform real inserts against `JobRunTiming` (a Beanie
    Document), which raises `CollectionWasNotInitialized` until `init_beanie`
    has run. Function-scoped rather than the shared `beanie_models` fixture in
    `tests/conftest.py` (which is `scope="module", loop_scope="module"`):
    pytest-asyncio hands each async test its own event loop by default, and a
    wider-scoped Motor client ends up bound to the wrong loop the moment a
    later test tries to use it -- same pattern as
    `tests/queue/test_cancel_cleanup.py`.

    Also drops `job_timings` on entry: several tests here assert exact counts
    for `job_type="align_reads"`, and leftover rows from an earlier test in
    this file would corrupt those counts without per-test isolation.
    """
    client = AsyncIOMotorClient(settings.mongo_url, tz_aware=True)
    db = client["biopipe_test"]
    await db[JobRunTiming.Settings.name].drop()
    await init_beanie(database=db, document_models=ALL_MODELS)
    yield
    client.close()


async def _record(outcome, duration_ms=120_000, input_bytes=1_000_000):
    await JobRunTiming(
        job_type="align_reads",
        input_bytes=input_bytes,
        duration_ms=duration_ms,
        outcome=outcome,
    ).insert()


class TestModelSamples:
    async def test_failed_runs_are_excluded_from_samples(self):
        for _ in range(6):
            await _record(RunOutcome.SUCCEEDED)
        for _ in range(4):
            await _record(RunOutcome.FAILED, duration_ms=500)
        samples = await timing_service._samples("align_reads")
        assert len(samples) == 6

    async def test_dead_and_cancelled_are_excluded_too(self):
        for _ in range(6):
            await _record(RunOutcome.SUCCEEDED)
        await _record(RunOutcome.DEAD, duration_ms=100)
        await _record(RunOutcome.CANCELLED, duration_ms=100)
        assert len(await timing_service._samples("align_reads")) == 6

    async def test_a_failed_run_does_not_drag_the_estimate_down(self):
        """The whole reason the filter exists."""
        for _ in range(8):
            await _record(RunOutcome.SUCCEEDED, duration_ms=120_000)
        clean = await timing_service.estimate("align_reads", 1_000_000)
        for _ in range(8):
            await _record(RunOutcome.FAILED, duration_ms=200)
        after = await timing_service.estimate("align_reads", 1_000_000)
        assert after["estimate_ms"] == pytest.approx(clean["estimate_ms"], rel=0.01)


class TestProvenance:
    async def test_provenance_includes_failures(self):
        """The one reader that wants them -- a failed run is the most useful
        record a user can read."""
        await JobRunTiming(
            job_type="align_reads",
            input_bytes=10,
            duration_ms=500,
            outcome=RunOutcome.FAILED,
            object_id="obj-1",
        ).insert()
        await JobRunTiming(
            job_type="align_reads",
            input_bytes=10,
            duration_ms=1000,
            outcome=RunOutcome.SUCCEEDED,
            object_id="obj-1",
        ).insert()
        records = await timing_service.records_for_object("obj-1")
        assert len(records) == 2
        assert {r.outcome for r in records} == {
            RunOutcome.FAILED,
            RunOutcome.SUCCEEDED,
        }

    async def test_provenance_is_scoped_to_the_object(self):
        await JobRunTiming(
            job_type="align_reads", input_bytes=10, duration_ms=1, object_id="obj-1"
        ).insert()
        await JobRunTiming(
            job_type="align_reads", input_bytes=10, duration_ms=1, object_id="obj-2"
        ).insert()
        assert len(await timing_service.records_for_object("obj-1")) == 1
