"""Carrying pre-#525 objects across the platform/instrument_model split.

Every object in the real database holds an instrument model in
`metadata.platform`, because that is what SRA enrichment wrote before the
split. The migration moves it to `instrument_model` and re-derives a real
SRA tag for `platform`.

The counts and values in `TestAgainstRealValues` are the ones actually
present on 2026-08-18 (55 objects over nine distinct models), taken from the
database rather than invented -- a fixture built from the migration's own
assumptions is exactly what would hide a break.
"""

import pytest
from beanie import PydanticObjectId, init_beanie
from pymongo import AsyncMongoClient

from app.config import settings
from app.metadata import platform_migration
from app.models import ALL_MODELS
from app.models.object import DataObject
from tests._mongo_isolation import direct_mongo_url, worker_db_name


@pytest.fixture(autouse=True)
async def _init_beanie_models():
    """Function-scoped for the reason tests/storage/test_read_pairing.py
    documents: a module-scoped client binds to the wrong event loop."""
    client = AsyncMongoClient(direct_mongo_url(settings.mongo_url), tz_aware=True)
    db = client[worker_db_name()]
    await init_beanie(database=db, document_models=ALL_MODELS)
    await DataObject.delete_all()
    yield
    await DataObject.delete_all()
    await client.close()


async def _saved(*, metadata: dict, facts: dict | None = None) -> DataObject:
    obj = DataObject(
        project_id=PydanticObjectId(),
        name="reads.fastq",
        metadata=metadata,
        facts=facts or {},
    )
    await obj.insert()
    return obj


class TestDerivePlatform:
    """Pure, so it is worth pinning separately from the write path."""

    @pytest.mark.parametrize(
        "model,expected",
        [
            ("Illumina NovaSeq X Plus", "ILLUMINA"),
            ("Illumina HiSeq 2000", "ILLUMINA"),
            ("NextSeq 550", "ILLUMINA"),
            ("Illumina MiSeq", "ILLUMINA"),
            ("MinION", "OXFORD_NANOPORE"),
            ("Sequel IIe", "PACBIO_SMRT"),
            ("PacBio RS", "PACBIO_SMRT"),
        ],
    )
    def test_real_stored_models_resolve(self, model, expected):
        """Every distinct value in the database on 2026-08-18."""
        assert platform_migration.derive_platform(model, sra_platform=None) == expected

    def test_the_sra_fact_wins_over_inference(self):
        """NCBI stamped it; nothing here can improve on that."""
        assert (
            platform_migration.derive_platform(
                "MinION", sra_platform="OXFORD_NANOPORE"
            )
            == "OXFORD_NANOPORE"
        )

    def test_an_unrecognized_model_resolves_to_none(self):
        """Not ILLUMINA. A wrong tag in a field `is_short_read` now trusts
        ahead of chemistry is the regression that path exists to prevent.

        Note the name has to avoid every substring in the pattern table --
        "Nanopore-ish 9000" resolves to OXFORD_NANOPORE, correctly, because
        `sam_platform` matches on substrings."""
        assert (
            platform_migration.derive_platform("Mystery Machine 5", sra_platform=None)
            is None
        )

    def test_a_value_already_a_tag_is_kept(self):
        assert (
            platform_migration.derive_platform("ILLUMINA", sra_platform=None)
            == "ILLUMINA"
        )


class TestMigration:
    async def test_moves_the_model_and_derives_the_tag(self):
        obj = await _saved(metadata={"platform": "NextSeq 550"})
        assert await platform_migration.split_platform_from_instrument_model() == 1

        after = await DataObject.get(obj.id)
        assert after.metadata["instrument_model"] == "NextSeq 550"
        assert after.metadata["platform"] == "ILLUMINA"

    async def test_the_sra_fact_is_preferred(self):
        obj = await _saved(
            metadata={"platform": "MinION"}, facts={"sra_platform": "OXFORD_NANOPORE"}
        )
        await platform_migration.split_platform_from_instrument_model()

        after = await DataObject.get(obj.id)
        assert after.metadata["platform"] == "OXFORD_NANOPORE"
        assert after.metadata["instrument_model"] == "MinION"

    async def test_an_unresolvable_model_clears_the_platform(self):
        """The model is still preserved, so nothing is lost -- and
        `_qc_platform` falls back to the same inference it would have done."""
        obj = await _saved(metadata={"platform": "Mystery Machine 5"})
        await platform_migration.split_platform_from_instrument_model()

        after = await DataObject.get(obj.id)
        assert after.metadata["instrument_model"] == "Mystery Machine 5"
        assert "platform" not in after.metadata

    async def test_other_metadata_is_untouched(self):
        obj = await _saved(
            metadata={"platform": "MinION", "organism": "Homo sapiens", "lane": 3}
        )
        await platform_migration.split_platform_from_instrument_model()

        after = await DataObject.get(obj.id)
        assert after.metadata["organism"] == "Homo sapiens"
        assert after.metadata["lane"] == 3

    async def test_running_twice_changes_nothing_the_second_time(self):
        """Idempotency comes from the data, not a flag: an object that
        already has `instrument_model` no longer matches the query."""
        obj = await _saved(metadata={"platform": "Sequel IIe"})
        assert await platform_migration.split_platform_from_instrument_model() == 1
        assert await platform_migration.split_platform_from_instrument_model() == 0

        after = await DataObject.get(obj.id)
        assert after.metadata["instrument_model"] == "Sequel IIe"
        assert after.metadata["platform"] == "PACBIO_SMRT"

    async def test_a_post_split_object_is_left_alone(self):
        """Already correct, and re-deriving would overwrite a user's own
        `instrument_model` with the tag sitting in `platform`."""
        obj = await _saved(
            metadata={"platform": "ILLUMINA", "instrument_model": "NextSeq 550"}
        )
        assert await platform_migration.split_platform_from_instrument_model() == 0

        after = await DataObject.get(obj.id)
        assert after.metadata["platform"] == "ILLUMINA"
        assert after.metadata["instrument_model"] == "NextSeq 550"

    async def test_an_object_with_no_platform_is_left_alone(self):
        obj = await _saved(metadata={"organism": "Homo sapiens"})
        assert await platform_migration.split_platform_from_instrument_model() == 0

        after = await DataObject.get(obj.id)
        assert "instrument_model" not in after.metadata

    async def test_an_empty_platform_is_not_migrated(self):
        """`""` would otherwise become an empty instrument model and lose
        the field's absence, which is a meaningful state."""
        obj = await _saved(metadata={"platform": ""})
        assert await platform_migration.split_platform_from_instrument_model() == 0

        after = await DataObject.get(obj.id)
        assert "instrument_model" not in after.metadata


class TestAgainstRealValues:
    async def test_the_whole_recorded_distribution_migrates(self):
        """The nine distinct `metadata.platform` values in the database on
        2026-08-18, at their real counts. Every one must resolve -- an
        unresolved row here would mean a real file losing its platform."""
        distribution = {
            "Illumina HiSeq 2000": 13,
            "MinION": 11,
            "Illumina HiSeq 4000": 10,
            "Illumina NovaSeq X Plus": 9,
            "Illumina NovaSeq 6000": 4,
            "Sequel IIe": 3,
            "Illumina MiSeq": 2,
            "NextSeq 550": 2,
            "PacBio RS": 1,
        }
        for model, count in distribution.items():
            for _ in range(count):
                await _saved(metadata={"platform": model})

        assert await platform_migration.split_platform_from_instrument_model() == 55

        migrated = await DataObject.find_all().to_list()
        assert len(migrated) == 55
        assert all("instrument_model" in o.metadata for o in migrated)
        assert all(o.metadata.get("platform") for o in migrated), (
            "a real file lost its platform"
        )

        tags = {}
        for o in migrated:
            tags[o.metadata["platform"]] = tags.get(o.metadata["platform"], 0) + 1
        assert tags == {"ILLUMINA": 40, "OXFORD_NANOPORE": 11, "PACBIO_SMRT": 4}
