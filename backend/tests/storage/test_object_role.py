"""Object role: the override that distinguishes a reference from reads."""

import pytest
from beanie import PydanticObjectId, init_beanie
from pymongo import AsyncMongoClient

from app.api.v1.schemas import ObjectOut, ObjectUpdate
from app.config import settings
from app.models import ALL_MODELS, FormatKind, ObjectRole
from app.models.object import DataObject
from app.services.object_service import apply_role_update


@pytest.fixture(scope="module", autouse=True)
async def _init_beanie_models():
    """Beanie requires init_beanie before *any* Document is instantiated, even
    without a save -- this is the only module in the suite that constructs a
    `DataObject` directly rather than through a fake, so the dependency is
    scoped here rather than to the whole test tree. Connects to the same
    Mongo the app uses but against a throwaway database, so it never touches
    real data.
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
    yield
    await client.close()


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


class TestRoleSerialization:
    def test_object_out_exposes_role(self):
        out = ObjectOut.of(_obj(role=ObjectRole.REFERENCE))
        assert out.role == "reference"

    def test_object_out_role_is_none_when_unset(self):
        assert ObjectOut.of(_obj()).role is None

    def test_update_distinguishes_omitted_from_explicit_null(self):
        """The whole reversibility story rests on this distinction.

        An omitted role must not appear in the dump (so a rename leaves role
        alone), while an explicit null must appear (so 'convert back' can clear
        it).
        """
        omitted = ObjectUpdate(name="x").model_dump(exclude_unset=True)
        assert "role" not in omitted

        explicit = ObjectUpdate(role=None).model_dump(exclude_unset=True)
        assert "role" in explicit
        assert explicit["role"] is None

    def test_update_accepts_a_role_value(self):
        dumped = ObjectUpdate(role=ObjectRole.REFERENCE).model_dump(exclude_unset=True)
        assert dumped["role"] is ObjectRole.REFERENCE


class TestApplyRoleUpdate:
    """Role is the one field where explicit-null differs from omitted."""

    def test_omitted_role_leaves_the_existing_value(self):
        obj = _obj(role=ObjectRole.REFERENCE)
        apply_role_update(obj, {"name": "renamed.fa"})
        assert obj.role is ObjectRole.REFERENCE

    def test_explicit_null_clears_the_role(self):
        obj = _obj(role=ObjectRole.REFERENCE)
        apply_role_update(obj, {"role": None})
        assert obj.role is None

    def test_setting_a_role(self):
        obj = _obj()
        apply_role_update(obj, {"role": ObjectRole.REFERENCE})
        assert obj.role is ObjectRole.REFERENCE

    def test_string_role_is_coerced_to_the_enum(self):
        """The route hands over whatever survived Pydantic; accept both."""
        obj = _obj()
        apply_role_update(obj, {"role": "reference"})
        assert obj.role is ObjectRole.REFERENCE

    def test_round_trip_conversion_returns_to_the_starting_state(self):
        obj = _obj()
        apply_role_update(obj, {"role": ObjectRole.REFERENCE})
        apply_role_update(obj, {"role": None})
        assert obj.role is None


class TestUserTouched:
    """Provenance for fields the user has explicitly set or cleared.

    Without this, a cleared role and a never-set role are the same value, and
    re-ingest cannot tell "no opinion" from "the user said no."
    """

    def test_defaults_to_empty(self):
        assert _obj().user_touched == []

    def test_records_a_field_name(self):
        obj = _obj(user_touched=["role"])
        assert "role" in obj.user_touched

    def test_setting_a_role_records_the_touch(self):
        obj = _obj()
        apply_role_update(obj, {"role": "reference"})
        assert obj.role is ObjectRole.REFERENCE
        assert obj.user_touched == ["role"]

    def test_clearing_a_role_records_the_touch(self):
        """The case the whole field exists for: a cleared role must be
        distinguishable from one that was never set."""
        obj = _obj(role=ObjectRole.REFERENCE)
        apply_role_update(obj, {"role": None})
        assert obj.role is None
        assert obj.user_touched == ["role"]

    def test_an_omitted_role_records_nothing(self):
        obj = _obj()
        apply_role_update(obj, {"name": "renamed.fasta"})
        assert obj.user_touched == []

    def test_the_touch_is_not_duplicated(self):
        obj = _obj()
        apply_role_update(obj, {"role": "reference"})
        apply_role_update(obj, {"role": None})
        assert obj.user_touched == ["role"]
