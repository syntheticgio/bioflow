"""Manual paired-end pairing: the override that filename inference cannot make."""

import pytest
from beanie import PydanticObjectId, init_beanie
from pymongo import AsyncMongoClient

from app.api.v1.schemas import ObjectOut, PairRequest
from app.config import settings
from app.errors import NotFoundError, ValidationError
from app.models import ALL_MODELS, ObjectRole, SidecarRole
from app.models.object import DataObject
from app.queue.results import _link_mate
from app.services import object_service


@pytest.fixture(autouse=True)
async def _init_beanie_models():
    """Beanie requires init_beanie before any Document is instantiated, and the
    client must be created inside the test's own event loop -- pytest-asyncio
    gives each test function a fresh loop, so a module-scoped client (bound to
    whichever loop happened to be running when the fixture first executed)
    raises "attached to a different loop" on every DB call. Function-scoped,
    same pattern as tests/queue/test_mate_link.py's `_db` fixture. Connects to
    the same Mongo the app uses but against a throwaway database, cleaned
    between tests rather than dropped, so index setup only has to run once
    per module import.
    """
    from app.db.index_reconcile import reconcile_indexes

    client = AsyncMongoClient(settings.mongo_url, tz_aware=True)
    db = client["biopipe_test"]
    for model in ALL_MODELS:
        model_settings = model.Settings
        coll_name = getattr(model_settings, "name", model.__name__.lower())
        indexes = getattr(model_settings, "indexes", [])
        if indexes:
            await reconcile_indexes(db[coll_name], indexes)
    await init_beanie(database=db, document_models=ALL_MODELS)
    await DataObject.delete_all()
    yield
    await DataObject.delete_all()
    await client.close()


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
            await object_service.set_pair(a.id, a.id, 1, owner="local")

    async def test_rejects_a_missing_mate(self):
        pid = PydanticObjectId()
        a = await _saved(pid, "a.fastq.gz")
        with pytest.raises(NotFoundError):
            await object_service.set_pair(a.id, PydanticObjectId(), 1, owner="local")

    async def test_rejects_a_mate_in_another_project(self):
        a = await _saved(PydanticObjectId(), "a.fastq.gz")
        b = await _saved(PydanticObjectId(), "b.fastq.gz")
        with pytest.raises(ValidationError, match="same project"):
            await object_service.set_pair(a.id, b.id, 1, owner="local")

    async def test_rejects_when_the_subject_is_already_paired(self):
        pid = PydanticObjectId()
        other = await _saved(pid, "other.fastq.gz")
        a = await _saved(pid, "a.fastq.gz", mate_object_id=other.id)
        b = await _saved(pid, "b.fastq.gz")
        with pytest.raises(ValidationError, match="already paired"):
            await object_service.set_pair(a.id, b.id, 1, owner="local")

    async def test_rejects_when_the_mate_is_already_paired(self):
        """Never displace a third file's pairing as a side effect."""
        pid = PydanticObjectId()
        third = await _saved(pid, "third.fastq.gz")
        a = await _saved(pid, "a.fastq.gz")
        b = await _saved(pid, "b.fastq.gz", mate_object_id=third.id)
        with pytest.raises(ValidationError, match="already paired"):
            await object_service.set_pair(a.id, b.id, 1, owner="local")

    async def test_rejects_a_reference(self):
        pid = PydanticObjectId()
        a = await _saved(pid, "a.fastq.gz")
        ref = await _saved(pid, "genome.fa", role=ObjectRole.REFERENCE)
        with pytest.raises(ValidationError, match="reads"):
            await object_service.set_pair(a.id, ref.id, 1, owner="local")

    async def test_rejects_a_sidecar(self):
        pid = PydanticObjectId()
        a = await _saved(pid, "a.fastq.gz")
        parent = await _saved(pid, "genome.fa")
        bai = await _saved(
            pid, "genome.fa.fai", sidecar_of=parent.id, sidecar_role=SidecarRole.FAI
        )
        with pytest.raises(ValidationError, match="reads"):
            await object_service.set_pair(a.id, bai.id, 1, owner="local")

    async def test_rejects_when_the_subject_is_a_reference(self):
        """Checked on both sides, not just the candidate."""
        pid = PydanticObjectId()
        ref = await _saved(pid, "genome.fa", role=ObjectRole.REFERENCE)
        b = await _saved(pid, "b.fastq.gz")
        with pytest.raises(ValidationError, match="reads"):
            await object_service.set_pair(ref.id, b.id, 1, owner="local")

    async def test_allows_trimmed_reads(self):
        """Trimmed output pairs like any other reads -- the point of the
        feature is files whose signals are missing, so over-filtering by
        format would recreate the gap it exists to close."""
        pid = PydanticObjectId()
        a = await _saved(pid, "a.trimmed.fastq.gz", role=ObjectRole.TRIMMED_READS)
        b = await _saved(pid, "b.trimmed.fastq.gz", role=ObjectRole.TRIMMED_READS)
        result = await object_service.set_pair(a.id, b.id, 1, owner="local")
        assert result.mate_object_id == b.id


class TestSetPairWrites:
    async def test_sets_both_pointers(self):
        pid = PydanticObjectId()
        a = await _saved(pid, "fwd.fastq.gz")
        b = await _saved(pid, "rev.fastq.gz")

        await object_service.set_pair(a.id, b.id, 1, owner="local")

        a_after = await DataObject.get(a.id)
        b_after = await DataObject.get(b.id)
        assert a_after.mate_object_id == b.id
        assert b_after.mate_object_id == a.id

    async def test_gives_the_mate_the_opposite_read_number(self):
        """The collision rule is structural: two R1s are unreachable."""
        pid = PydanticObjectId()
        a = await _saved(pid, "fwd.fastq.gz")
        b = await _saved(pid, "rev.fastq.gz")

        await object_service.set_pair(a.id, b.id, 1, owner="local")

        assert (await DataObject.get(a.id)).read_number == 1
        assert (await DataObject.get(b.id)).read_number == 2

    async def test_read_number_two_flips_the_other_way(self):
        pid = PydanticObjectId()
        a = await _saved(pid, "rev.fastq.gz")
        b = await _saved(pid, "fwd.fastq.gz")

        await object_service.set_pair(a.id, b.id, 2, owner="local")

        assert (await DataObject.get(a.id)).read_number == 2
        assert (await DataObject.get(b.id)).read_number == 1

    async def test_marks_both_sides_user_touched(self):
        """The durable record that stops re-ingest from overriding this."""
        pid = PydanticObjectId()
        a = await _saved(pid, "fwd.fastq.gz")
        b = await _saved(pid, "rev.fastq.gz")

        await object_service.set_pair(a.id, b.id, 1, owner="local")

        assert "mate" in (await DataObject.get(a.id)).user_touched
        assert "mate" in (await DataObject.get(b.id)).user_touched

    async def test_does_not_duplicate_the_touch(self):
        """$addToSet, so pairing a file that was paired before stays clean."""
        pid = PydanticObjectId()
        a = await _saved(pid, "fwd.fastq.gz", user_touched=["mate"])
        b = await _saved(pid, "rev.fastq.gz")

        await object_service.set_pair(a.id, b.id, 1, owner="local")

        assert (await DataObject.get(a.id)).user_touched.count("mate") == 1

    async def test_preserves_an_existing_role_touch(self):
        """user_touched is shared across fields; pairing must not clobber it."""
        pid = PydanticObjectId()
        a = await _saved(pid, "fwd.fastq.gz", user_touched=["role"])
        b = await _saved(pid, "rev.fastq.gz")

        await object_service.set_pair(a.id, b.id, 1, owner="local")

        touched = (await DataObject.get(a.id)).user_touched
        assert "role" in touched
        assert "mate" in touched

    async def test_returns_the_updated_subject(self):
        pid = PydanticObjectId()
        a = await _saved(pid, "fwd.fastq.gz")
        b = await _saved(pid, "rev.fastq.gz")

        result = await object_service.set_pair(a.id, b.id, 1, owner="local")

        assert result.mate_object_id == b.id
        assert result.read_number == 1

    async def test_a_lost_race_leaves_no_half_link(self):
        """If the mate gets paired between validation and the write, the
        subject must not be left pointing at it."""
        pid = PydanticObjectId()
        third = await _saved(pid, "third.fastq.gz")
        a = await _saved(pid, "fwd.fastq.gz")
        b = await _saved(pid, "rev.fastq.gz")

        # Validation reads both as unpaired, then the mate is taken.
        obj = await object_service.get_object(a.id, owner="local")
        await b.set({DataObject.mate_object_id: third.id})

        with pytest.raises(ValidationError):
            await object_service.set_pair(obj.id, b.id, 1, owner="local")

        assert (await DataObject.get(a.id)).mate_object_id is None
        assert (await DataObject.get(a.id)).read_number is None


class TestClearPair:
    async def test_clears_both_pointers(self):
        pid = PydanticObjectId()
        a = await _saved(pid, "fwd.fastq.gz")
        b = await _saved(pid, "rev.fastq.gz")
        await object_service.set_pair(a.id, b.id, 1, owner="local")

        await object_service.clear_pair(a.id, owner="local")

        assert (await DataObject.get(a.id)).mate_object_id is None
        assert (await DataObject.get(b.id)).mate_object_id is None

    async def test_clears_both_read_numbers(self):
        """A read number outliving its pair would collide against a value the
        user believed they had cleared."""
        pid = PydanticObjectId()
        a = await _saved(pid, "fwd.fastq.gz")
        b = await _saved(pid, "rev.fastq.gz")
        await object_service.set_pair(a.id, b.id, 1, owner="local")

        await object_service.clear_pair(a.id, owner="local")

        assert (await DataObject.get(a.id)).read_number is None
        assert (await DataObject.get(b.id)).read_number is None

    async def test_keeps_user_touched_on_both_sides(self):
        """The cleared state is itself the user's decision -- this entry is
        what stops re-ingest from undoing it."""
        pid = PydanticObjectId()
        a = await _saved(pid, "fwd.fastq.gz")
        b = await _saved(pid, "rev.fastq.gz")
        await object_service.set_pair(a.id, b.id, 1, owner="local")

        await object_service.clear_pair(a.id, owner="local")

        assert "mate" in (await DataObject.get(a.id)).user_touched
        assert "mate" in (await DataObject.get(b.id)).user_touched

    async def test_clearing_from_the_other_side_works_too(self):
        """Pairing is symmetric, so unpair must be reachable from either file."""
        pid = PydanticObjectId()
        a = await _saved(pid, "fwd.fastq.gz")
        b = await _saved(pid, "rev.fastq.gz")
        await object_service.set_pair(a.id, b.id, 1, owner="local")

        await object_service.clear_pair(b.id, owner="local")

        assert (await DataObject.get(a.id)).mate_object_id is None
        assert (await DataObject.get(b.id)).mate_object_id is None

    async def test_is_a_no_op_on_an_unpaired_object(self):
        """Idempotent, so a double click is harmless."""
        pid = PydanticObjectId()
        a = await _saved(pid, "lonely.fastq.gz")

        result = await object_service.clear_pair(a.id, owner="local")

        assert result.mate_object_id is None
        assert result.user_touched == []

    async def test_a_dangling_mate_pointer_still_clears(self):
        """The mate row being gone must not block unpairing the survivor."""
        pid = PydanticObjectId()
        a = await _saved(pid, "fwd.fastq.gz", mate_object_id=PydanticObjectId(), read_number=1)

        result = await object_service.clear_pair(a.id, owner="local")

        assert result.mate_object_id is None
        assert result.read_number is None

    async def test_can_re_pair_after_clearing(self):
        """The correction path: unpair, then pair with the right file."""
        pid = PydanticObjectId()
        a = await _saved(pid, "fwd.fastq.gz")
        wrong = await _saved(pid, "wrong.fastq.gz")
        right = await _saved(pid, "right.fastq.gz")
        await object_service.set_pair(a.id, wrong.id, 1, owner="local")

        await object_service.clear_pair(a.id, owner="local")
        await object_service.set_pair(a.id, right.id, 1, owner="local")

        assert (await DataObject.get(a.id)).mate_object_id == right.id
        assert (await DataObject.get(right.id)).read_number == 2
        assert (await DataObject.get(wrong.id)).mate_object_id is None


class TestInferenceRespectsManualPairing:
    """Filename inference must never overrule a person -- the same promise
    user_touched already keeps for role."""

    async def test_a_manual_pairing_survives_reingest(self):
        """The motivating case: names say one thing, the user said another."""
        pid = PydanticObjectId()
        # Names that inference *would* pair with each other.
        r1 = await _saved(pid, "s_R1.fastq.gz")
        r2 = await _saved(pid, "s_R2.fastq.gz")
        # But the user pairs R1 with an unconventionally named file instead.
        odd = await _saved(pid, "sampleA_forward.fastq.gz")
        await object_service.set_pair(r1.id, odd.id, 1, owner="local")

        await _link_mate(await DataObject.get(r1.id))

        assert (await DataObject.get(r1.id)).mate_object_id == odd.id
        assert (await DataObject.get(r2.id)).mate_object_id is None

    async def test_a_cleared_pairing_is_not_re_inferred(self):
        """The hole in the old guard: cleared looks identical to never-set."""
        pid = PydanticObjectId()
        r1 = await _saved(pid, "s_R1.fastq.gz")
        r2 = await _saved(pid, "s_R2.fastq.gz")
        await object_service.set_pair(r1.id, r2.id, 1, owner="local")
        await object_service.clear_pair(r1.id, owner="local")

        await _link_mate(await DataObject.get(r1.id))

        assert (await DataObject.get(r1.id)).mate_object_id is None
        assert (await DataObject.get(r2.id)).mate_object_id is None

    async def test_inference_does_not_pair_into_a_cleared_file(self):
        """Reached from the *other* side: r2 was never touched, so it is a
        candidate -- but r1 was, and must not be pulled back in."""
        pid = PydanticObjectId()
        r1 = await _saved(pid, "s_R1.fastq.gz", user_touched=["mate"])
        r2 = await _saved(pid, "s_R2.fastq.gz")

        await _link_mate(await DataObject.get(r2.id))

        assert (await DataObject.get(r2.id)).mate_object_id is None
        assert (await DataObject.get(r1.id)).mate_object_id is None

    async def test_untouched_files_still_pair_automatically(self):
        """The guard must not break ordinary inference."""
        pid = PydanticObjectId()
        r1 = await _saved(pid, "auto_R1.fastq.gz")
        r2 = await _saved(pid, "auto_R2.fastq.gz")

        await _link_mate(await DataObject.get(r2.id))

        assert (await DataObject.get(r1.id)).mate_object_id == r2.id
        assert (await DataObject.get(r2.id)).mate_object_id == r1.id

    async def test_inference_records_read_numbers(self):
        """split_mate already computes R1/R2 and throws it away; keeping it
        gives inferred pairs their badges for free."""
        pid = PydanticObjectId()
        r1 = await _saved(pid, "auto_R1.fastq.gz")
        r2 = await _saved(pid, "auto_R2.fastq.gz")

        await _link_mate(await DataObject.get(r2.id))

        assert (await DataObject.get(r1.id)).read_number == 1
        assert (await DataObject.get(r2.id)).read_number == 2

    async def test_inferred_pairing_is_not_marked_user_touched(self):
        """Only a person's choice earns the override; inference does not."""
        pid = PydanticObjectId()
        r1 = await _saved(pid, "auto_R1.fastq.gz")
        r2 = await _saved(pid, "auto_R2.fastq.gz")

        await _link_mate(await DataObject.get(r2.id))

        assert (await DataObject.get(r1.id)).user_touched == []
        assert (await DataObject.get(r2.id)).user_touched == []
