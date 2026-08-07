"""The alignment reservation, which is what claim.lua gates on.

Distinct from the advisory sites: a wrong number here is silently costly in
both directions -- too low over-admits, too high starves the queue.
"""

import pytest
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from app.config import settings
from app.models import ALL_MODELS
from app.models.timing import JobRunTiming, RunOutcome, RunResources
from app.pipelines.aligners import Aligner
from app.services import pipeline_service
from app.services.pipeline_service import MIN_DECLARED_MEM_MB


@pytest.fixture(autouse=True)
async def _init_beanie_models():
    client = AsyncIOMotorClient(settings.mongo_url, tz_aware=True)
    db = client["biopipe_test"]
    await db[JobRunTiming.Settings.name].drop()
    await init_beanie(database=db, document_models=ALL_MODELS)
    yield
    client.close()


class TestDeclaredAlignMem:
    async def test_floor_holds_when_the_measured_model_predicts_almost_nothing(self):
        """A reference whose size is missing, or a measured model fit on tiny
        runs, would otherwise reserve almost nothing and let the governor admit
        the job alongside everything else -- the exact reason the floor exists."""
        for i in range(1, 9):
            await JobRunTiming(
                job_type="align_reads",
                input_bytes=1_000_000 * i,
                duration_ms=120_000,
                outcome=RunOutcome.SUCCEEDED,
                resources=RunResources(peak_rss_bytes=1_000_000 + 1000 * i),
            ).insert()

        mem_mb = await pipeline_service.declared_align_mem_mb(
            aligner=Aligner.BWA_MEM2,
            reference_bases=0,
            threads=4,
            sort_memory_mb=256,
            building_index=False,
            input_bytes=4_000_000,
        )

        assert mem_mb >= MIN_DECLARED_MEM_MB

    async def test_no_history_still_reserves_the_heuristic_number(self):
        """Behaviour before this change, preserved: with no rows, the
        coefficients are still the reservation."""
        mem_mb = await pipeline_service.declared_align_mem_mb(
            aligner=Aligner.BWA_MEM2,
            reference_bases=3_000_000_000,
            threads=8,
            sort_memory_mb=1024,
            building_index=False,
            input_bytes=None,
        )

        assert mem_mb > MIN_DECLARED_MEM_MB
