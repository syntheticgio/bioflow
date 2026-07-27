"""Sidecars: scaffolding attached to a file, distinct from files derived from it.

The distinction carries real weight. A trimmed FASTQ is a specimen -- something
you search, annotate and align -- while a `.bwt` is biologically inert and means
nothing away from its reference. That difference decides what the explorer shows
and, more consequentially, what deletion destroys.
"""

import pytest
from beanie import PydanticObjectId, init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from app.config import settings
from app.models import ALL_MODELS, ObjectRole, SidecarRole
from app.models.object import DataObject


@pytest.fixture(scope="module")
async def beanie_models():
    """Beanie refuses to instantiate a Document before init_beanie, even for an
    object that is never saved. Mirrors the fixture in test_object_role.py and
    uses the same throwaway database, so this never touches real data.

    Requested explicitly rather than autouse: it needs a running Mongo, and
    most of the decisions in this file are plain enum and index assertions that
    should not be dragged behind a database dependency they do not have.
    """
    client = AsyncIOMotorClient(settings.mongo_url, tz_aware=True)
    await init_beanie(database=client["biopipe_test"], document_models=ALL_MODELS)
    yield
    client.close()


class TestSidecarRole:
    def test_covers_both_index_shapes(self):
        """minimap2's index is a single .mmi and bwa-mem2's is a five-file set.
        Both exist from the start so the sidecar model cannot quietly harden
        around BWA's shape."""
        assert SidecarRole.BWA_MEM2_INDEX.value == "bwa-mem2-index"
        assert SidecarRole.MINIMAP2_INDEX.value == "minimap2-index"

    def test_covers_the_samtools_sidecars(self):
        assert SidecarRole.FAI.value == "fai"
        assert SidecarRole.BAI.value == "bai"

    def test_is_a_string_enum(self):
        """Stored and compared as a plain string, like the other role enums."""
        assert SidecarRole.FAI == "fai"


class TestAlignmentRole:
    def test_alignment_is_a_role_not_a_format(self):
        """A produced BAM and an uploaded BAM are the same format and differ
        only in whether their provenance is known, so format cannot carry it."""
        assert ObjectRole.ALIGNMENT.value == "alignment"

    def test_does_not_collide_with_existing_roles(self):
        values = [r.value for r in ObjectRole]
        assert len(values) == len(set(values))


class TestSidecarFields:
    """These construct a DataObject and so need Beanie initialized."""

    def test_default_to_none(self, beanie_models):
        """Every object that predates sidecars reads as a non-sidecar, which is
        what keeps the explorer listing unchanged for existing data."""
        obj = DataObject(project_id=PydanticObjectId(), name="genome.fna")
        assert obj.sidecar_of is None
        assert obj.sidecar_role is None

    def test_a_sidecar_records_both_its_parent_and_its_kind(self, beanie_models):
        parent = PydanticObjectId()
        obj = DataObject(
            project_id=PydanticObjectId(),
            name="genome.fna.bwt",
            sidecar_of=parent,
            sidecar_role=SidecarRole.BWA_MEM2_INDEX,
        )
        assert obj.sidecar_of == parent
        assert obj.sidecar_role is SidecarRole.BWA_MEM2_INDEX

    def test_sidecar_of_is_independent_of_derived_from(self, beanie_models):
        """The two relationships must not be conflated: they mean different
        things and, critically, delete differently."""
        parent = PydanticObjectId()
        obj = DataObject(
            project_id=PydanticObjectId(),
            name="genome.fna.fai",
            sidecar_of=parent,
            sidecar_role=SidecarRole.FAI,
        )
        assert obj.derived_from == []

    def test_a_derived_file_is_not_a_sidecar(self, beanie_models):
        """A trimmed FASTQ descends from its input but accompanies nothing, so
        it stays visible in the explorer and survives its parent's deletion."""
        source = PydanticObjectId()
        obj = DataObject(
            project_id=PydanticObjectId(),
            name="sample_R1.trimmed.fastq.gz",
            role=ObjectRole.TRIMMED_READS,
            derived_from=[source],
        )
        assert obj.sidecar_of is None


class TestIndexing:
    """Read off the class rather than a live collection, so these run anywhere."""

    def test_sidecar_of_is_indexed(self):
        """'Does this reference have an index?' runs on every alignment launch
        and on every explorer render of a reference."""
        assert "by_sidecar_of" in _index_names()

    def test_derived_from_is_still_indexed(self):
        assert "by_derived_from" in _index_names()


def _index_names() -> set[str]:
    return {idx.document["name"] for idx in DataObject.Settings.indexes}
