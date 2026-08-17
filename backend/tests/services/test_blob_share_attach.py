import pytest
from app.errors import NotFoundError
from app.models import Blob, BlobState, BlobStorage, DataObject, ObjectStatus
from app.services import blob_service
from beanie import PydanticObjectId

pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


async def _external_blob(digest: str) -> Blob:
    blob = Blob(
        id=digest,
        size=10,
        state=BlobState.PRESENT,
        storage=BlobStorage.EXTERNAL,
        external_path=f"/data/ext/{digest}.fa",
        ref_count=1,
        observed_size=10,
        observed_mtime=1_700_000_000.0,
    )
    await blob.insert()
    return blob


async def test_share_attach_preserves_the_external_drift_baseline():
    digest = "d" * 64
    await _external_blob(digest)
    obj = DataObject(project_id=PydanticObjectId(), name="ref.fa", owner="b")
    await obj.insert()

    await blob_service.attach_existing_blob_to_object(object_id=obj.id, digest=digest, size=10)

    blob = await Blob.get(digest)
    assert blob.ref_count == 2
    # The whole point: these are untouched.
    assert blob.observed_mtime == 1_700_000_000.0
    assert blob.observed_size == 10


async def test_share_attach_does_not_claim_a_verification_it_did_not_do():
    digest = "e" * 64
    blob = await _external_blob(digest)
    before = blob.last_verified_at
    obj = DataObject(project_id=PydanticObjectId(), name="ref.fa", owner="b")
    await obj.insert()

    await blob_service.attach_existing_blob_to_object(object_id=obj.id, digest=digest, size=10)

    assert (await Blob.get(digest)).last_verified_at == before


async def test_share_attach_refuses_a_blob_that_is_not_present():
    digest = "f" * 64
    blob = await _external_blob(digest)
    blob.state = BlobState.QUARANTINED
    await blob.save()
    obj = DataObject(project_id=PydanticObjectId(), name="ref.fa", owner="b")
    await obj.insert()

    with pytest.raises(NotFoundError):
        await blob_service.attach_existing_blob_to_object(object_id=obj.id, digest=digest, size=10)
    assert (await Blob.get(digest)).ref_count == 1  # not incremented


async def test_share_attach_refuses_a_digest_that_does_not_exist():
    digest = "9" * 64
    obj = DataObject(project_id=PydanticObjectId(), name="ref.fa", owner="b")
    await obj.insert()

    with pytest.raises(NotFoundError):
        await blob_service.attach_existing_blob_to_object(object_id=obj.id, digest=digest, size=10)


async def test_share_attach_sets_the_object_ready():
    digest = "0" * 64
    await _external_blob(digest)
    obj = DataObject(project_id=PydanticObjectId(), name="ref.fa", owner="b")
    await obj.insert()

    await blob_service.attach_existing_blob_to_object(object_id=obj.id, digest=digest, size=10)
    refreshed = await DataObject.get(obj.id)
    assert refreshed.status is ObjectStatus.READY
    assert refreshed.blob_sha256 == digest
