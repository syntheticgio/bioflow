"""Manual paired-end pairing: the override that filename inference cannot make."""

import pytest
from beanie import PydanticObjectId, init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from app.api.v1.schemas import ObjectOut, PairRequest
from app.config import settings
from app.errors import NotFoundError, ValidationError
from app.models import ALL_MODELS, ObjectRole, SidecarRole
from app.models.object import DataObject
from app.services import object_service


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


async def _saved(project_id: PydanticObjectId, name: str, **kw) -> DataObject:
    """A DataObject persisted to the throwaway test database."""
    obj = DataObject(project_id=project_id, name=name, **kw)
    await obj.insert()
    return obj


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


class TestPairRequest:
    """read_number is validated at the edge, so the service can trust it."""

    def test_accepts_one_and_two(self):
        mate = PydanticObjectId()
        assert PairRequest(mate_object_id=mate, read_number=1).read_number == 1
        assert PairRequest(mate_object_id=mate, read_number=2).read_number == 2

    def test_rejects_zero(self):
        with pytest.raises(ValueError):
            PairRequest(mate_object_id=PydanticObjectId(), read_number=0)

    def test_rejects_three(self):
        with pytest.raises(ValueError):
            PairRequest(mate_object_id=PydanticObjectId(), read_number=3)

    def test_requires_a_mate(self):
        with pytest.raises(ValueError):
            PairRequest(read_number=1)


class TestSetPairValidation:
    """Every rejection the endpoint can produce, in the order they are checked.

    Strict by design: correcting a wrong pairing is unpair-then-pair, so no
    request can ever displace a third file's mate as a side effect.
    """

    async def test_rejects_pairing_with_itself(self):
        pid = PydanticObjectId()
        a = await _saved(pid, "a.fastq.gz")
        with pytest.raises(ValidationError, match="itself"):
            await object_service.set_pair(a.id, a.id, 1)

    async def test_rejects_a_missing_mate(self):
        pid = PydanticObjectId()
        a = await _saved(pid, "a.fastq.gz")
        with pytest.raises(NotFoundError):
            await object_service.set_pair(a.id, PydanticObjectId(), 1)

    async def test_rejects_a_mate_in_another_project(self):
        a = await _saved(PydanticObjectId(), "a.fastq.gz")
        b = await _saved(PydanticObjectId(), "b.fastq.gz")
        with pytest.raises(ValidationError, match="same project"):
            await object_service.set_pair(a.id, b.id, 1)

    async def test_rejects_when_the_subject_is_already_paired(self):
        pid = PydanticObjectId()
        other = await _saved(pid, "other.fastq.gz")
        a = await _saved(pid, "a.fastq.gz", mate_object_id=other.id)
        b = await _saved(pid, "b.fastq.gz")
        with pytest.raises(ValidationError, match="already paired"):
            await object_service.set_pair(a.id, b.id, 1)

    async def test_rejects_when_the_mate_is_already_paired(self):
        """Never displace a third file's pairing as a side effect."""
        pid = PydanticObjectId()
        third = await _saved(pid, "third.fastq.gz")
        a = await _saved(pid, "a.fastq.gz")
        b = await _saved(pid, "b.fastq.gz", mate_object_id=third.id)
        with pytest.raises(ValidationError, match="already paired"):
            await object_service.set_pair(a.id, b.id, 1)

    async def test_rejects_a_reference(self):
        pid = PydanticObjectId()
        a = await _saved(pid, "a.fastq.gz")
        ref = await _saved(pid, "genome.fa", role=ObjectRole.REFERENCE)
        with pytest.raises(ValidationError, match="reads"):
            await object_service.set_pair(a.id, ref.id, 1)

    async def test_rejects_a_sidecar(self):
        pid = PydanticObjectId()
        a = await _saved(pid, "a.fastq.gz")
        parent = await _saved(pid, "genome.fa")
        bai = await _saved(
            pid, "genome.fa.fai", sidecar_of=parent.id, sidecar_role=SidecarRole.FAI
        )
        with pytest.raises(ValidationError, match="reads"):
            await object_service.set_pair(a.id, bai.id, 1)

    async def test_rejects_when_the_subject_is_a_reference(self):
        """Checked on both sides, not just the candidate."""
        pid = PydanticObjectId()
        ref = await _saved(pid, "genome.fa", role=ObjectRole.REFERENCE)
        b = await _saved(pid, "b.fastq.gz")
        with pytest.raises(ValidationError, match="reads"):
            await object_service.set_pair(ref.id, b.id, 1)

    async def test_allows_trimmed_reads(self):
        """Trimmed output pairs like any other reads -- the point of the
        feature is files whose signals are missing, so over-filtering by
        format would recreate the gap it exists to close."""
        pid = PydanticObjectId()
        a = await _saved(pid, "a.trimmed.fastq.gz", role=ObjectRole.TRIMMED_READS)
        b = await _saved(pid, "b.trimmed.fastq.gz", role=ObjectRole.TRIMMED_READS)
        result = await object_service.set_pair(a.id, b.id, 1)
        assert result.mate_object_id == b.id
