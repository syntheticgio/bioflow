"""Object role: the override that distinguishes a reference from reads."""

import pytest
from app.config import settings
from app.models import ALL_MODELS, FormatKind, ObjectRole
from app.models.object import DataObject
from beanie import PydanticObjectId, init_beanie
from motor.motor_asyncio import AsyncIOMotorClient


@pytest.fixture(scope="module", autouse=True)
async def _init_beanie_models():
    """Beanie requires init_beanie before *any* Document is instantiated, even
    without a save -- this is the only module in the suite that constructs a
    `DataObject` directly rather than through a fake, so the dependency is
    scoped here rather than to the whole test tree. Connects to the same
    Mongo the app uses but against a throwaway database, so it never touches
    real data.
    """
    client = AsyncIOMotorClient(settings.mongo_url, tz_aware=True)
    await init_beanie(database=client["biopipe_test"], document_models=ALL_MODELS)
    yield
    client.close()


def _obj(**kw) -> DataObject:
    """A DataObject built without touching the database."""
    defaults = dict(project_id=PydanticObjectId(), name="sample.fastq.gz")
    return DataObject(**{**defaults, **kw})


class TestObjectRole:
    def test_role_defaults_to_none(self):
        """None means 'derive the category from the format', today's behavior."""
        assert _obj().role is None

    def test_role_accepts_reference(self):
        assert _obj(role=ObjectRole.REFERENCE).role is ObjectRole.REFERENCE

    def test_role_is_a_string_enum(self):
        """StrEnum so it serializes to plain JSON without a custom encoder."""
        assert ObjectRole.REFERENCE == "reference"
        assert _obj(role="reference").role is ObjectRole.REFERENCE

    def test_role_round_trips_through_serialization(self):
        dumped = _obj(role=ObjectRole.REFERENCE).model_dump(mode="json")
        assert dumped["role"] == "reference"
        assert _obj(**{"role": dumped["role"]}).role is ObjectRole.REFERENCE

    def test_format_kind_is_independent_of_role(self):
        """A reference can be FASTQ; the whole point is that format does not decide."""
        o = _obj(role=ObjectRole.REFERENCE)
        o.format.kind = FormatKind.FASTQ
        assert o.role is ObjectRole.REFERENCE
        assert o.format.kind is FormatKind.FASTQ
