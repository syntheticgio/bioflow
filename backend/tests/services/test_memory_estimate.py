"""The layered memory estimate resolver.

Tests assert the *falling-back* direction wherever a guard is involved. Per
CLAUDE.md, the passing direction proves nothing here: the resolver returns a
number in almost every arrangement, so a test that only checks "we got an
estimate" passes whether or not the guard it claims to test is wired up.
"""

import pytest
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from app.config import settings
from app.models import ALL_MODELS
from app.models.timing import JobRunTiming, RunOutcome, RunResources
from app.services import memory_estimate
from app.services.memory_estimate import EstimateSource


@pytest.fixture(autouse=True)
async def _init_beanie_models():
    """Function-scoped Beanie init, and drops `job_timings` on entry.

    Same reasoning as `tests/storage/test_memory_model.py`: pytest-asyncio
    hands each async test its own event loop, so a wider-scoped Motor client
    ends up bound to the wrong loop; and these tests assert on exact
    resolution outcomes, which leftover rows from an earlier test would
    corrupt.
    """
    client = AsyncIOMotorClient(settings.mongo_url, tz_aware=True)
    db = client["biopipe_test"]
    await db[JobRunTiming.Settings.name].drop()
    await init_beanie(database=db, document_models=ALL_MODELS)
    yield
    client.close()


class TestUnknown:
    async def test_no_history_and_no_heuristic_is_unknown(self):
        """The assembly-without-genome-size case. `estimate_assembly_mb`
        returns None on purpose there, and the caller must let the run
        proceed rather than refuse -- so the resolver must not invent a
        number."""
        result = await memory_estimate.resolve(
            job_type="never_seen_job",
            input_bytes=1_000_000,
            heuristic_mb=None,
        )
        assert result.source is EstimateSource.UNKNOWN
        assert result.mb is None


async def _insert_runs(job_type: str, count: int = 8, *, peak_base: int = 10_000_000):
    """A well-behaved history: a clean linear relationship, all succeeded.

    Returns the largest input size inserted, so callers can ask about an input
    inside or beyond the observed range without recomputing it.
    """
    for i in range(1, count + 1):
        await JobRunTiming(
            job_type=job_type,
            input_bytes=1_000_000 * i,
            duration_ms=120_000,
            outcome=RunOutcome.SUCCEEDED,
            resources=RunResources(peak_rss_bytes=peak_base + 1_000_000 * i),
        ).insert()
    return 1_000_000 * count


class TestHeuristic:
    async def test_heuristic_is_used_when_there_is_no_history(self):
        result = await memory_estimate.resolve(
            job_type="never_seen_job",
            input_bytes=1_000_000,
            heuristic_mb=4096,
        )
        assert result.source is EstimateSource.HEURISTIC
        assert result.mb == 4096
        assert result.fell_back_from_measured is False


class TestMeasured:
    async def test_measured_wins_over_the_heuristic_in_range(self):
        """The graduation the whole feature exists for: once a job type has
        real history on this machine, coefficients stop being the answer."""
        await _insert_runs("measured_win_job")

        result = await memory_estimate.resolve(
            job_type="measured_win_job",
            input_bytes=5_000_000,
            heuristic_mb=99_999,
        )

        assert result.source is EstimateSource.MEASURED
        assert result.mb != 99_999
        assert result.mb > 0
        assert result.samples == 8
        assert "previous runs" in result.detail

    async def test_measured_reports_megabytes_not_bytes(self):
        """`estimate_memory` returns bytes; every caller of this resolver
        works in MB. A unit mismatch here would be a 1,048,576x error that
        still looks like a plausible integer."""
        await _insert_runs("measured_units_job", peak_base=2 * 1024**3)

        result = await memory_estimate.resolve(
            job_type="measured_units_job",
            input_bytes=5_000_000,
            heuristic_mb=None,
        )

        assert result.source is EstimateSource.MEASURED
        # ~2 GB of peak RSS is ~2048 MB, not ~2.1 billion.
        assert 1500 < result.mb < 3000
