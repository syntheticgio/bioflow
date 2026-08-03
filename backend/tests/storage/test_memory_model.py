"""Peak-memory prediction from measured runs.

Most of this is pure arithmetic, tested without Mongo -- matching
test_timing_model.py, which tests the duration fit the same way and for the
same reason. `TestEstimateMemory` is the exception: it exercises the actual
async `estimate_memory()` entry point, including the `_modelled()`
outcome-filtered read that keeps failed/OOM-killed runs out of the fit.
"""

from types import SimpleNamespace

import pytest
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from app.config import settings
from app.models import ALL_MODELS
from app.models.timing import JobRunTiming, RunOutcome, RunResources
from app.services import timing_service
from app.services.timing_service import MIN_SAMPLES, _fit_memory, _memory_samples_from

# No `pytestmark = pytest.mark.asyncio` needed: pyproject.toml sets
# `asyncio_mode = "auto"`, so bare `async def` tests are collected.


@pytest.fixture(autouse=True)
async def _init_beanie_models():
    """Beanie raises `CollectionWasNotInitialized` on any real insert until
    `init_beanie` has run. Function-scoped rather than the shared
    `beanie_models` fixture in `tests/conftest.py` (which is
    `scope="module", loop_scope="module"`): pytest-asyncio hands each async
    test its own event loop by default, and a wider-scoped Motor client ends
    up bound to the wrong loop the moment a later test tries to use it --
    same pattern as `tests/queue/test_record_outcomes.py`.

    Also drops `job_timings` on entry: `TestEstimateMemory` asserts exact
    sample counts per job type, and leftover rows from an earlier test would
    corrupt those counts without per-test isolation.
    """
    client = AsyncIOMotorClient(settings.mongo_url, tz_aware=True)
    db = client["biopipe_test"]
    await db[JobRunTiming.Settings.name].drop()
    await init_beanie(database=db, document_models=ALL_MODELS)
    yield
    client.close()


class TestInsufficientData:
    def test_no_samples_gives_no_model(self):
        assert _fit_memory([]) is None

    @pytest.mark.parametrize("n", range(1, MIN_SAMPLES))
    def test_below_threshold_gives_no_model(self, n):
        """Same silence-before-confidence rule as the duration model: a
        confidently wrong memory number invites an OOM."""
        assert _fit_memory([(1000 * i, 100 * i) for i in range(1, n + 1)]) is None


class TestFit:
    def test_recovers_a_known_slope(self):
        """1 byte of RSS per byte of input, plus 100 MB fixed."""
        base = 100 * 1024 * 1024
        samples = [(1_000_000 * i, base + 1_000_000 * i) for i in range(1, 21)]
        model = _fit_memory(samples)
        assert model["slope"] == pytest.approx(1.0, rel=1e-6)
        assert model["intercept"] == pytest.approx(base, rel=1e-6)

    def test_constant_memory_yields_a_flat_model(self):
        """Many tools have a footprint set by the reference, not the reads --
        a flat model is the right answer, not a failure."""
        samples = [(1_000_000 * i, 2 * 1024**3) for i in range(1, 21)]
        model = _fit_memory(samples)
        assert model["flat"] is True
        assert model["intercept"] == pytest.approx(2 * 1024**3, rel=1e-6)


class TestExclusions:
    def test_samples_without_a_peak_are_dropped_before_fitting(self):
        """Runs under the sampling floor carry None, not zero. Treating them
        as zero would drag every prediction toward nothing."""
        records = [
            SimpleNamespace(input_bytes=1000, resources=SimpleNamespace(peak_rss_bytes=None)),
            SimpleNamespace(input_bytes=2000, resources=SimpleNamespace(peak_rss_bytes=5000)),
            SimpleNamespace(input_bytes=3000, resources=SimpleNamespace(peak_rss_bytes=0)),
        ]
        assert _memory_samples_from(records) == [(2000, 5000)]


class TestEstimateMemory:
    """The async public API, `estimate_memory()`. Unlike the classes above,
    these go through real Mongo -- the point is to exercise `_modelled()`,
    the outcome-filtered accessor, rather than the pure functions it feeds.
    """

    async def test_no_records_yields_known_false(self):
        result = await timing_service.estimate_memory("nonexistent_job_type", 1000)
        assert result["known"] is False
        assert result["samples"] == 0

    async def test_enough_measured_runs_yields_a_real_estimate(self):
        for i in range(1, 9):
            await JobRunTiming(
                job_type="memory_test_job",
                input_bytes=1_000_000 * i,
                duration_ms=120_000,
                outcome=RunOutcome.SUCCEEDED,
                resources=RunResources(peak_rss_bytes=10_000_000 + 1_000_000 * i),
            ).insert()

        result = await timing_service.estimate_memory("memory_test_job", 5_000_000)

        assert result["known"] is True
        assert result["estimate_bytes"] > 0
        assert result["samples"] == 8
        assert "r_squared" in result

    async def test_a_failed_run_is_excluded_from_the_memory_fit(self):
        """The whole reason estimate_memory reads via _modelled rather than
        querying JobRunTiming directly."""
        for i in range(1, 9):
            await JobRunTiming(
                job_type="memory_fail_test",
                input_bytes=1_000_000 * i,
                duration_ms=120_000,
                outcome=RunOutcome.SUCCEEDED,
                resources=RunResources(peak_rss_bytes=10_000_000 + 1_000_000 * i),
            ).insert()
        clean = await timing_service.estimate_memory("memory_fail_test", 5_000_000)

        # An OOM-killed run with a huge peak -- if this leaked into the fit,
        # the estimate would move.
        await JobRunTiming(
            job_type="memory_fail_test",
            input_bytes=5_000_000,
            duration_ms=1000,
            outcome=RunOutcome.FAILED,
            resources=RunResources(peak_rss_bytes=999_000_000_000),
        ).insert()
        after = await timing_service.estimate_memory("memory_fail_test", 5_000_000)

        assert after["estimate_bytes"] == clean["estimate_bytes"]
        assert after["samples"] == clean["samples"] == 8
