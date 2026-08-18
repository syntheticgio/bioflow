"""Locality is what makes an offloaded object distinguishable from a broken one.

The whole design of #523 rests on `locality` carrying remoteness while
`status` keeps meaning what it always meant. These tests pin both halves:
the default that makes every pre-existing document correct without a
migration, and the independence of the two fields.
"""

import pytest
from beanie import PydanticObjectId

from app.models import DataObject, Locality, ObjectStatus, RemoteSource

pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


def _obj(**overrides) -> DataObject:
    base = dict(
        project_id=PydanticObjectId(),
        owner="local",
        name="ERR17407954_1.fastq.gz",
        size=1234,
    )
    return DataObject(**{**base, **overrides})


async def test_locality_defaults_to_local():
    """Every object that existed before this field is LOCAL, with no migration."""
    obj = _obj()
    assert obj.locality is Locality.LOCAL
    assert obj.remote_source is None


async def test_a_document_with_no_locality_key_reads_back_as_local():
    """The default must survive the round trip, not just the constructor.

    A stored document written before this field existed has no `locality`
    key at all. Reading it must not raise and must not report REMOTE --
    which would offload every legacy object at once.
    """
    obj = _obj()
    await obj.insert()
    await DataObject.get_pymongo_collection().update_one(
        {"_id": obj.id}, {"$unset": {"locality": "", "remote_source": ""}}
    )
    reloaded = await DataObject.get(obj.id)
    assert reloaded is not None
    assert reloaded.locality is Locality.LOCAL
    assert reloaded.remote_source is None


async def test_offloading_does_not_change_status():
    """`status` stays READY; `locality` is the only field that moves.

    This is the decision the whole feature rests on. The reference picker
    and the Actions rules filter on READY -- the latter at the query layer,
    where a non-READY object is excluded in the database and never reaches
    Python to be rescued.
    """
    obj = _obj(status=ObjectStatus.READY, blob_sha256="a" * 64)
    await obj.insert()

    obj.locality = Locality.REMOTE
    obj.remote_source = RemoteSource(accession="ERR17407954", size=98765)
    obj.blob_sha256 = None
    await obj.save()

    reloaded = await DataObject.get(obj.id)
    assert reloaded is not None
    assert reloaded.status is ObjectStatus.READY
    assert reloaded.locality is Locality.REMOTE
    assert reloaded.blob_sha256 is None


async def test_remote_source_records_the_refetch_address():
    """`component` is nullable: an SRA run has none, a future assembly will."""
    source = RemoteSource(accession="ERR17407954", size=98765)
    assert source.accession == "ERR17407954"
    assert source.component is None
    assert source.size == 98765


async def test_a_remote_object_is_still_queryable_as_ready():
    """The regression this design exists to avoid, at the query layer.

    Stage 3 tests this through the picker and the suggestion rules; here it
    is pinned against the database directly, because a query-layer filter is
    where a status-based design would have failed silently.
    """
    project_id = PydanticObjectId()
    remote = _obj(
        project_id=project_id,
        status=ObjectStatus.READY,
        locality=Locality.REMOTE,
        remote_source=RemoteSource(accession="ERR17407954", size=1),
    )
    await remote.insert()

    found = await DataObject.find(
        DataObject.project_id == project_id,
        DataObject.status == ObjectStatus.READY,
    ).to_list()
    assert [o.id for o in found] == [remote.id]
