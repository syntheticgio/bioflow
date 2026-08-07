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
