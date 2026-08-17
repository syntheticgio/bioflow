"""Failed runs are recorded for provenance and excluded from every fit.

The bug this guards against is silent and points the wrong way: an OOM kill at
ninety seconds reads as a fast, cheap run whose peak RSS is the ceiling it hit
rather than what it needed. A few in a fit drag estimates down -- toward
causing the next OOM.
"""

import pytest
from beanie import init_beanie
from pymongo import AsyncMongoClient

from app.config import settings
from app.models import ALL_MODELS
from app.models.timing import JobRunTiming, RunOutcome
from app.services import timing_service
from app.services.timing_service import MIN_SAMPLES

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
    client = AsyncMongoClient(settings.mongo_url, tz_aware=True)
    db = client["biopipe_test"]
    await db[JobRunTiming.Settings.name].drop()
    await init_beanie(database=db, document_models=ALL_MODELS)
    yield
    await client.close()


async def _record(outcome, duration_ms=120_000, input_bytes=1_000_000, threads=None):
    await JobRunTiming(
        job_type="align_reads",
        input_bytes=input_bytes,
        duration_ms=duration_ms,
        outcome=outcome,
        threads=threads,
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

    async def test_limit_returns_the_newest_records(self):
        from datetime import UTC, datetime, timedelta

        base = datetime.now(UTC)
        for i in range(3):
            await JobRunTiming(
                job_type="align_reads",
                input_bytes=10,
                duration_ms=1,
                object_id="obj-1",
                finished_at=base + timedelta(seconds=i),
            ).insert()

        records = await timing_service.records_for_object("obj-1", limit=2)
        assert len(records) == 2
        # Mongo round-trips finished_at at millisecond precision, so compare
        # ordering rather than exact equality against the microsecond values
        # this test constructed.
        assert records[0].finished_at > records[1].finished_at
        assert records[1].finished_at - base > timedelta(milliseconds=500)

    async def test_no_limit_returns_everything(self):
        for _ in range(3):
            await JobRunTiming(
                job_type="align_reads", input_bytes=10, duration_ms=1, object_id="obj-1"
            ).insert()
        assert len(await timing_service.records_for_object("obj-1")) == 3


class TestThreadSegmentation:
    async def test_no_threads_argument_matches_todays_bytes_only_behavior(self):
        """Regression pin: estimate() with no threads arg must be identical
        to before this feature existed."""
        for i in range(1, MIN_SAMPLES + 1):
            await _record(
                RunOutcome.SUCCEEDED, duration_ms=1000 * i, input_bytes=1000 * i
            )
        result = await timing_service.estimate("align_reads", 5000)
        assert result["known"] is True
        assert result["segment"] == {"threads": None, "samples": result["samples"]}

    async def test_segment_with_enough_samples_answers_over_the_pool(self):
        """A thread count with its own MIN_SAMPLES rows and a distinct slope
        must be the one that answers, not the pooled fit."""
        for i in range(1, MIN_SAMPLES + 1):
            await _record(
                RunOutcome.SUCCEEDED,
                duration_ms=1 * i,
                input_bytes=1000 * i,
                threads=4,
            )
        for i in range(1, MIN_SAMPLES + 1):
            await _record(
                RunOutcome.SUCCEEDED,
                duration_ms=3 * i,
                input_bytes=1000 * i,
                threads=8,
            )
        result = await timing_service.estimate("align_reads", 5000, threads=8)
        assert result["segment"]["threads"] == 8

    async def test_segment_r_squared_is_scored_against_its_own_samples(self):
        """A segment's confidence score must reflect how well ITS OWN samples
        fit its own model, not how the pooled all-threads samples fit it --
        mixing two different slopes into one pooled comparison would produce
        a misleadingly low r_squared for a segment that is actually a
        perfect fit."""
        for i in range(1, MIN_SAMPLES + 1):
            await _record(
                RunOutcome.SUCCEEDED,
                duration_ms=1 * i,
                input_bytes=1000 * i,
                threads=4,
            )
        for i in range(1, MIN_SAMPLES + 1):
            await _record(
                RunOutcome.SUCCEEDED,
                duration_ms=3 * i,
                input_bytes=1000 * i,
                threads=8,
            )
        result = await timing_service.estimate("align_reads", 5000, threads=8)
        assert result["segment"]["threads"] == 8
        assert result["r_squared"] > 0.99

    async def test_sparse_thread_count_falls_back(self):
        """Fewer than MIN_SAMPLES rows at the requested thread count: the
        answer comes from the None fallback, not a half-formed segment."""
        for i in range(1, MIN_SAMPLES + 1):
            await _record(
                RunOutcome.SUCCEEDED,
                duration_ms=1000 * i,
                input_bytes=1000 * i,
                threads=4,
            )
        for _ in range(2):
            await _record(RunOutcome.SUCCEEDED, duration_ms=1, threads=16)
        result = await timing_service.estimate("align_reads", 5000, threads=16)
        assert result["segment"]["threads"] is None

    async def test_a_failed_run_at_the_segment_thread_count_is_excluded(self):
        """Outcome filtering must hold under segmentation too -- a FAILED row
        at a thread count that would otherwise qualify for its own segment
        must not enter that segment's fit or the fallback."""
        for _ in range(MIN_SAMPLES):
            await _record(
                RunOutcome.SUCCEEDED,
                duration_ms=120_000,
                input_bytes=1_000_000,
                threads=4,
            )
        clean = await timing_service.estimate("align_reads", 1_000_000, threads=4)
        for _ in range(MIN_SAMPLES):
            await _record(
                RunOutcome.FAILED, duration_ms=200, input_bytes=1_000_000, threads=4
            )
        after = await timing_service.estimate("align_reads", 1_000_000, threads=4)
        assert after["estimate_ms"] == pytest.approx(clean["estimate_ms"], rel=0.01)

    async def test_memory_estimate_segment_selection_mirrors_duration(self):
        for i in range(1, MIN_SAMPLES + 1):
            await JobRunTiming(
                job_type="align_reads",
                input_bytes=1000 * i,
                duration_ms=1,
                outcome=RunOutcome.SUCCEEDED,
                threads=4,
                resources={"peak_rss_bytes": 1_000_000 * i},
            ).insert()
        # A different-slope threads=8 pool alongside it -- proves r_squared
        # is scored against the threads=4 segment's own samples, not the
        # pooled (two-slope) set, the same bug shape as the duration side.
        for i in range(1, MIN_SAMPLES + 1):
            await JobRunTiming(
                job_type="align_reads",
                input_bytes=1000 * i,
                duration_ms=1,
                outcome=RunOutcome.SUCCEEDED,
                threads=8,
                resources={"peak_rss_bytes": 3_000_000 * i},
            ).insert()
        result = await timing_service.estimate_memory(
            "align_reads", 5000, threads=4
        )
        assert result["known"] is True
        assert result["segment"]["threads"] == 4
        assert result["r_squared"] > 0.99


class TestStatsSegments:
    async def test_segments_list_is_empty_with_no_thread_data(self):
        for _ in range(MIN_SAMPLES):
            await _record(RunOutcome.SUCCEEDED)
        rows = await timing_service.stats()
        row = next(r for r in rows if r["job_type"] == "align_reads")
        assert row["segments"] == []

    async def test_segments_list_reports_a_qualifying_thread_count(self):
        for i in range(1, MIN_SAMPLES + 1):
            await _record(
                RunOutcome.SUCCEEDED,
                duration_ms=1000 * i,
                input_bytes=1000 * i,
                threads=4,
            )
        rows = await timing_service.stats()
        row = next(r for r in rows if r["job_type"] == "align_reads")
        assert len(row["segments"]) == 1
        assert row["segments"][0]["threads"] == 4
        assert row["segments"][0]["samples"] == MIN_SAMPLES
        assert row["segments"][0]["model"] is not None

    async def test_sparse_thread_count_is_omitted_from_segments(self):
        for _ in range(MIN_SAMPLES - 1):
            await _record(RunOutcome.SUCCEEDED, threads=4)
        rows = await timing_service.stats()
        row = next(r for r in rows if r["job_type"] == "align_reads")
        assert row["segments"] == []

    async def test_segment_r_squared_in_stats_is_scored_against_its_own_samples(self):
        """stats()'s segment r_squared must reflect how well a thread count's
        OWN samples fit its own model, not the pooled all-threads samples --
        same bug shape Task 3 fixed in estimate()/estimate_memory()."""
        for i in range(1, MIN_SAMPLES + 1):
            await _record(
                RunOutcome.SUCCEEDED,
                duration_ms=1 * i,
                input_bytes=1000 * i,
                threads=4,
            )
        for i in range(1, MIN_SAMPLES + 1):
            await _record(
                RunOutcome.SUCCEEDED,
                duration_ms=3 * i,
                input_bytes=1000 * i,
                threads=8,
            )
        rows = await timing_service.stats()
        row = next(r for r in rows if r["job_type"] == "align_reads")
        segment_by_threads = {s["threads"]: s for s in row["segments"]}
        assert len(segment_by_threads) == 2
        assert segment_by_threads[4]["model"]["r_squared"] > 0.99
        assert segment_by_threads[8]["model"]["r_squared"] > 0.99
