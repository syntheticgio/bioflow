"""Manual paired-end pairing: the override that filename inference cannot make."""

import pytest
from beanie import PydanticObjectId, init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from app.api.v1.schemas import ObjectOut
from app.config import settings
from app.models import ALL_MODELS
from app.models.object import DataObject


@pytest.fixture(scope="module", autouse=True)
async def _init_beanie_models():
    """Beanie requires init_beanie before any Document is instantiated. Connects
    to the same Mongo the app uses but against a throwaway database."""
    from app.db.index_reconcile import reconcile_indexes

    client = AsyncIOMotorClient(settings.mongo_url, tz_aware=True)
    db = client["biopipe_test"]
    for model in ALL_MODELS:
        model_settings = model.Settings
        coll_name = getattr(model_settings, "name", model.__name__.lower())
        indexes = getattr(model_settings, "indexes", [])
        if indexes:
            await reconcile_indexes(db[coll_name], indexes)
    await init_beanie(database=db, document_models=ALL_MODELS)
    yield
    client.close()


def _obj(**kw) -> DataObject:
    """A DataObject built without touching the database."""
    defaults = dict(project_id=PydanticObjectId(), name="sample.fastq.gz")
    return DataObject(**{**defaults, **kw})


class TestReadNumberField:
    def test_defaults_to_none(self):
        """Unpaired and unknown are the same state: no read number."""
        assert _obj().read_number is None

    def test_accepts_one_and_two(self):
        assert _obj(read_number=1).read_number == 1
        assert _obj(read_number=2).read_number == 2

    def test_round_trips_through_serialization(self):
        dumped = _obj(read_number=2).model_dump(mode="json")
        assert dumped["read_number"] == 2
        assert _obj(**{"read_number": dumped["read_number"]}).read_number == 2


class TestReadNumberSerialization:
    def test_object_out_exposes_read_number(self):
        assert ObjectOut.of(_obj(read_number=1)).read_number == 1

    def test_object_out_read_number_is_none_when_unset(self):
        assert ObjectOut.of(_obj()).read_number is None
