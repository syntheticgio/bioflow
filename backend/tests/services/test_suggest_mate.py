"""`suggest_mate` must not propose a pair `verdict()` would veto.

If validation lands only in `results._link_mate`, the launch dialog keeps
suggesting a pairing ingest already rejected -- this is the second call site
the spec calls out as easy to miss.
"""

import pytest
from app.config import settings
from app.models.object import DataObject, ObjectStatus
from app.services.pipeline_service import suggest_mate
from beanie import init_beanie
from pymongo import AsyncMongoClient


@pytest.fixture
async def _db():
    client = AsyncMongoClient(settings.mongo_url, tz_aware=True)
    db = client["biopipe_test"]
    await init_beanie(database=db, document_models=[DataObject])
    await DataObject.delete_all()
    yield db
    await DataObject.delete_all()
    await client.close()


async def _obj(
    name: str,
    project_id="507f1f77bcf86cd799439011",
    facts: dict | None = None,
    metadata: dict | None = None,
) -> DataObject:
    o = DataObject(
        project_id=project_id,
        name=name,
        size=1024,
        status=ObjectStatus.READY,
        facts=facts or {},
        metadata=metadata or {},
    )
    await o.insert()
    return o


class TestSuggestMate:
    async def test_vetoed_pair_returns_none(self, _db):
        a = await _obj("foo_1.fastq", metadata={"read_type": "single-end"})
        await _obj("foo_2.fastq", metadata={"read_type": "single-end"})

        assert await suggest_mate(a) is None

    async def test_valid_pair_is_suggested(self, _db):
        a = await _obj("sample_R1.fastq.gz")
        b = await _obj("sample_R2.fastq.gz")

        suggested = await suggest_mate(a)
        assert suggested is not None
        assert suggested.id == b.id

    async def test_already_linked_mate_is_preferred_over_filename(self, _db):
        a = await _obj("foo_1.fastq")
        b = await _obj("foo_2.fastq")
        a.mate_object_id = b.id
        await a.save()

        suggested = await suggest_mate(a)
        assert suggested is not None
        assert suggested.id == b.id
