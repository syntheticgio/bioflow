"""Mate linking sets both the pointer and the read number.

The badge in the file list is driven by `read_number` while the spine that
connects the two rows is driven by `mate_object_id`. Both come from the same
`pairing.split_mate` call for a reason: if they could disagree, the UI would
claim two files are one run while labelling both of them R1. The final test
here is the one that pins that invariant.
"""

import pytest
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from app.config import settings
from app.models.object import DataObject, ObjectStatus
from app.queue.results import _link_mate


@pytest.fixture
async def _db():
    """Throwaway test database, same pattern as tests/db/test_index_reconcile."""
    client = AsyncIOMotorClient(settings.mongo_url, tz_aware=True)
    db = client["biopipe_test"]
    await init_beanie(database=db, document_models=[DataObject])
    await DataObject.delete_all()
    yield db
    await DataObject.delete_all()
    client.close()


async def _obj(
    name: str,
    project_id="507f1f77bcf86cd799439011",
    facts: dict | None = None,
    metadata: dict | None = None,
) -> DataObject:
    """A ready FASTQ object carrying just enough to be linkable.

    `owner` is inherited from TimestampedDocument and defaults to "local", so it
    is left alone. Enum members are upper-case: ObjectStatus.READY.
    """
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


class TestLinkMate:
    async def test_links_r_scheme_pair_with_read_numbers(self, _db):
        r1 = await _obj("sample_R1.fastq.gz")
        r2 = await _obj("sample_R2.fastq.gz")

        # The second file to arrive is the one that finds the match.
        await _link_mate(r2)

        r1, r2 = await DataObject.get(r1.id), await DataObject.get(r2.id)
        assert r1.mate_object_id == r2.id
        assert r2.mate_object_id == r1.id
        assert r1.read_number == 1
        assert r2.read_number == 2

    async def test_links_numeric_scheme_pair(self, _db):
        a = await _obj("sample_1.fastq.gz")
        b = await _obj("sample_2.fastq.gz")

        await _link_mate(b)

        a, b = await DataObject.get(a.id), await DataObject.get(b.id)
        assert a.read_number == 1
        assert b.read_number == 2

    async def test_unpaired_file_gets_no_read_number(self, _db):
        solo = await _obj("sample.fastq.gz")

        await _link_mate(solo)

        solo = await DataObject.get(solo.id)
        assert solo.mate_object_id is None
        assert solo.read_number is None

    async def test_read_numbers_never_collide_within_a_pair(self, _db):
        """The invariant the badge depends on.

        Asserted directly rather than inferred from the naming cases above: any
        linked pair must carry one 1 and one 2, whatever the filenames were.
        """
        r1 = await _obj("Sample_R1.fastq")
        r2 = await _obj("sample_R2.fastq")

        await _link_mate(r2)

        r1, r2 = await DataObject.get(r1.id), await DataObject.get(r2.id)
        assert r1.mate_object_id is not None
        assert {r1.read_number, r2.read_number} == {1, 2}


class TestLinkMateVerdict:
    async def test_single_end_layout_is_not_linked(self, _db):
        a = await _obj("foo_1.fastq", metadata={"read_type": "single-end"})
        b = await _obj("foo_2.fastq", metadata={"read_type": "single-end"})

        await _link_mate(b)

        a, b = await DataObject.get(a.id), await DataObject.get(b.id)
        assert a.mate_object_id is None
        assert b.mate_object_id is None

    async def test_conflicting_read_ids_are_not_linked(self, _db):
        a = await _obj("foo_1.fastq", facts={"first_read_ids": ["ERR1.1 length=150"]})
        b = await _obj("foo_2.fastq", facts={"first_read_ids": ["SRR2.1 length=100"]})

        await _link_mate(b)

        a, b = await DataObject.get(a.id), await DataObject.get(b.id)
        assert a.mate_object_id is None
        assert b.mate_object_id is None

    async def test_real_shaped_valid_pair_still_links(self, _db):
        a = await _obj(
            "ERR17609896_1.fastq",
            facts={
                "first_read_ids": [
                    "ERR17609896.1 LH00201:115:22JLCTLT4:1:1101:22244:1112 length=150",
                ]
            },
        )
        b = await _obj(
            "ERR17609896_2.fastq",
            facts={
                "first_read_ids": [
                    "ERR17609896.1 LH00201:115:22JLCTLT4:1:1101:22244:1112 length=149",
                ]
            },
        )

        await _link_mate(b)

        a, b = await DataObject.get(a.id), await DataObject.get(b.id)
        assert a.mate_object_id == b.id
        assert b.mate_object_id == a.id

    async def test_both_signals_absent_still_links(self, _db):
        """The fast path: unchanged behavior when neither signal exists."""
        a = await _obj("sample_R1.fastq.gz")
        b = await _obj("sample_R2.fastq.gz")

        await _link_mate(b)

        a, b = await DataObject.get(a.id), await DataObject.get(b.id)
        assert a.mate_object_id == b.id
        assert b.mate_object_id == a.id
