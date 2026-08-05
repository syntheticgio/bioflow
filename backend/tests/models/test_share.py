import pytest
from beanie import PydanticObjectId

from app.models import DataObject, Share, ShareState

pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


async def test_share_defaults_to_offered():
    share = Share(
        from_owner="local",
        to_owner="65f000000000000000000001",
        source_object_id=PydanticObjectId(),
        name="reads.fastq.gz",
        size=1234,
        blob_sha256="a" * 64,
    )
    await share.insert()
    assert share.state is ShareState.OFFERED
    assert share.accepted_object_id is None


async def test_duplicate_pending_offer_is_rejected():
    """The unique partial index is the guard, not a service-level read."""
    from pymongo.errors import DuplicateKeyError

    source = PydanticObjectId()
    common = dict(
        from_owner="local",
        to_owner="65f000000000000000000002",
        source_object_id=source,
        name="ref.fna",
        size=99,
        blob_sha256="b" * 64,
    )
    await Share(**common).insert()
    with pytest.raises(DuplicateKeyError):
        await Share(**common).insert()


async def test_a_declined_offer_does_not_block_re_offering():
    """Only OFFERED participates in the index, so history never blocks a retry."""
    source = PydanticObjectId()
    common = dict(
        from_owner="local",
        to_owner="65f000000000000000000003",
        source_object_id=source,
        name="ref.fna",
        size=99,
        blob_sha256="c" * 64,
    )
    first = Share(**common)
    await first.insert()
    first.state = ShareState.DECLINED
    await first.save()

    await Share(**common).insert()  # must not raise


async def test_shared_from_is_a_typed_field_not_metadata():
    obj = DataObject(project_id=PydanticObjectId(), name="x.bam")
    assert obj.shared_from is None
