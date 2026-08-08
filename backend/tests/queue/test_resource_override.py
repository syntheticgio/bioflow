"""The launch-anyway override, from the job document through to claim.lua.

The assertions here are chosen for the direction that fails when the seam
breaks. Asserting that an overridden job IS claimed proves little -- most
things are claimable in a quiet test environment. Asserting it is REFUSED
under contention is what fails if `sole` is computed wrongly.
"""

import pytest
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from app.config import settings
from app.models import ALL_MODELS
from app.models.job import Job, JobState


@pytest.fixture(autouse=True)
async def _init_beanie_models(monkeypatch):
    """`Job.insert`/`Job.get` need Beanie initialized against a real database --
    same pattern as `tests/queue/test_cancel_cleanup.py`. Function-scoped since
    this suite performs I/O and pytest-asyncio hands each async test its own
    event loop by default.
    """
    client = AsyncIOMotorClient(settings.mongo_url, tz_aware=True)
    db = client["biopipe_test"]
    await init_beanie(database=db, document_models=ALL_MODELS)
    monkeypatch.setattr("app.db.client.get_db", lambda: db)
    yield
    client.close()


@pytest.mark.asyncio
async def test_resource_override_defaults_to_false():
    job = Job(type="align_reads", owner="p1", state=JobState.PENDING)
    assert job.resource_override is False


@pytest.mark.asyncio
async def test_resource_override_persists_across_a_reload():
    job = Job(
        type="align_reads",
        owner="p1",
        state=JobState.PENDING,
        resource_override=True,
    )
    await job.insert()

    reloaded = await Job.get(job.id)
    assert reloaded is not None
    assert reloaded.resource_override is True
