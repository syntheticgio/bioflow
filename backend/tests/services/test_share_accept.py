"""share_service.accept_share -- the materialization cascade.

The two assertions that carry weight, per the plan: `derived_from`/
`produced_by_job` must be cleared on the copy (a naive `model_copy()` passes
every other test here and fails only this one), and a copied sidecar's
`sidecar_of` must point at the NEW parent, not the source's -- copying it
verbatim leaves a sidecar the recipient's owner-scoped `list_sidecars` can
never find.
"""

import pytest
from beanie import PydanticObjectId

from app.errors import ConflictError, NotFoundError
from app.models import Blob, DataObject, ObjectStatus, SidecarRole
from app.services import blob_service, object_service, project_service, share_service
from tests.services.helpers_share import make_profile, ready_object, scratch_file

pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


@pytest.fixture(autouse=True)
def _no_queue(monkeypatch):
    async def _skip(obj, **kwargs):
        return ""

    monkeypatch.setattr(object_service, "enqueue_ingest", _skip)


@pytest.fixture(autouse=True)
def _no_events(monkeypatch):
    from app.queue import queue

    async def _skip(*args, **kwargs):
        return None

    monkeypatch.setattr(queue, "publish_event", _skip)


@pytest.fixture(autouse=True, scope="module")
def _cleanup_scratch():
    yield
    from tests.services.helpers_share import reclaim_scratch_files

    reclaim_scratch_files()


async def _offer_and_load(*, sender: str, recipient: str, obj: DataObject):
    share = await share_service.offer_share(
        owner=sender, object_id=obj.id, to_profile_id=recipient
    )
    return share


async def test_accepting_creates_a_second_object_on_the_same_blob():
    sender = await make_profile("accept-basic-sender")
    recipient = await make_profile("accept-basic-recipient")
    obj = await ready_object(owner=sender)
    blob_before = await Blob.get(obj.blob_sha256)

    share = await _offer_and_load(sender=sender, recipient=recipient, obj=obj)
    copy = await share_service.accept_share(owner=recipient, share_id=share.id)

    assert copy.owner == recipient
    assert copy.blob_sha256 == obj.blob_sha256
    assert copy.id != obj.id
    blob_after = await Blob.get(obj.blob_sha256)
    assert blob_after.ref_count == blob_before.ref_count + 1


async def test_the_copy_clears_cross_partition_provenance():
    sender = await make_profile("accept-prov-sender")
    recipient = await make_profile("accept-prov-recipient")
    project = await project_service.create_project(name="prov-src", owner=sender)
    parent = await ready_object(owner=sender, project=project)
    derived = await object_service.ingest_local_file(
        project_id=project.id,
        path=scratch_file(b"derived-bytes"),
        name="derived.txt",
        owner=sender,
        derived_from=[parent.id],
        produced_by_job=PydanticObjectId(),
    )
    await derived.set({DataObject.status: ObjectStatus.READY})
    derived = await object_service.get_object(derived.id, owner=sender)
    assert derived.derived_from == [parent.id]
    assert derived.produced_by_job is not None

    share = await _offer_and_load(sender=sender, recipient=recipient, obj=derived)
    copy = await share_service.accept_share(owner=recipient, share_id=share.id)

    assert copy.derived_from == []
    assert copy.produced_by_job is None


async def test_the_copy_records_shared_from():
    sender = await make_profile("accept-sf-sender")
    recipient = await make_profile("accept-sf-recipient")
    obj = await ready_object(owner=sender)

    share = await _offer_and_load(sender=sender, recipient=recipient, obj=obj)
    copy = await share_service.accept_share(owner=recipient, share_id=share.id)

    assert copy.shared_from is not None
    assert copy.shared_from.object_id == obj.id
    assert copy.shared_from.owner == sender
    assert copy.shared_from.share_id == share.id


async def test_a_shared_bam_brings_its_bai_repointed_at_the_new_parent():
    sender = await make_profile("accept-bai-sender")
    recipient = await make_profile("accept-bai-recipient")
    project = await project_service.create_project(name="bai-src", owner=sender)
    bam = await ready_object(owner=sender, project=project)
    bai = await object_service.ingest_local_file(
        project_id=project.id,
        path=scratch_file(b"index-bytes"),
        name=f"{bam.name}.bai",
        owner=sender,
        sidecar_of=bam.id,
        sidecar_role=SidecarRole.BAI,
    )

    share = await _offer_and_load(sender=sender, recipient=recipient, obj=bam)
    bam_copy = await share_service.accept_share(owner=recipient, share_id=share.id)

    sidecars = await object_service.list_sidecars(bam_copy.id, owner=recipient)
    assert len(sidecars) == 1
    assert sidecars[0].sidecar_of == bam_copy.id
    assert sidecars[0].sidecar_of != bai.sidecar_of or bam_copy.id != bam.id
    assert sidecars[0].id != bai.id
    assert sidecars[0].sidecar_role == SidecarRole.BAI


async def test_shared_paired_reads_point_at_each_other_not_at_the_source():
    sender = await make_profile("accept-pair-sender")
    recipient = await make_profile("accept-pair-recipient")
    project = await project_service.create_project(name="pair-src", owner=sender)
    r1 = await object_service.ingest_local_file(
        project_id=project.id, path=scratch_file(b"r1-bytes"), name="r1.fastq", owner=sender
    )
    r2 = await object_service.ingest_local_file(
        project_id=project.id, path=scratch_file(b"r2-bytes"), name="r2.fastq", owner=sender
    )
    await object_service.set_pair(r1.id, r2.id, read_number=1, owner=sender)
    await r1.set({DataObject.status: ObjectStatus.READY})
    r1 = await object_service.get_object(r1.id, owner=sender)

    share = await _offer_and_load(sender=sender, recipient=recipient, obj=r1)
    r1_copy = await share_service.accept_share(owner=recipient, share_id=share.id)

    assert r1_copy.mate_object_id is not None
    assert r1_copy.mate_object_id != r2.id
    mate_copy = await object_service.get_object(r1_copy.mate_object_id, owner=recipient)
    assert mate_copy.mate_object_id == r1_copy.id
    assert mate_copy.owner == recipient


async def test_accepting_twice_is_refused():
    sender = await make_profile("accept-twice-sender")
    recipient = await make_profile("accept-twice-recipient")
    obj = await ready_object(owner=sender)

    share = await _offer_and_load(sender=sender, recipient=recipient, obj=obj)
    await share_service.accept_share(owner=recipient, share_id=share.id)

    with pytest.raises(ConflictError):
        await share_service.accept_share(owner=recipient, share_id=share.id)


async def test_a_wrong_recipient_cannot_accept():
    sender = await make_profile("accept-wrong-sender")
    recipient = await make_profile("accept-wrong-recipient")
    stranger = await make_profile("accept-wrong-stranger")
    obj = await ready_object(owner=sender)

    share = await _offer_and_load(sender=sender, recipient=recipient, obj=obj)

    with pytest.raises(NotFoundError):
        await share_service.accept_share(owner=stranger, share_id=share.id)


async def test_destination_project_must_belong_to_the_recipient():
    sender = await make_profile("accept-destproj-sender")
    recipient = await make_profile("accept-destproj-recipient")
    stranger = await make_profile("accept-destproj-stranger")
    obj = await ready_object(owner=sender)
    strangers_project = await project_service.create_project(
        name="strangers", owner=stranger
    )

    share = await _offer_and_load(sender=sender, recipient=recipient, obj=obj)

    with pytest.raises(NotFoundError):
        await share_service.accept_share(
            owner=recipient, share_id=share.id, project_id=strangers_project.id
        )


async def test_counters_move_on_the_destination_project():
    sender = await make_profile("accept-counters-sender")
    recipient = await make_profile("accept-counters-recipient")
    obj = await ready_object(owner=sender, content=b"12345")
    dest = await project_service.create_project(name="counters-dest", owner=recipient)

    share = await _offer_and_load(sender=sender, recipient=recipient, obj=obj)
    await share_service.accept_share(owner=recipient, share_id=share.id, project_id=dest.id)

    refreshed = await project_service.get_project(dest.id, owner=recipient)
    assert refreshed.counters.object_count == 1
    assert refreshed.counters.total_bytes == obj.size


async def test_accepting_when_the_source_was_deleted_is_refused():
    sender = await make_profile("accept-deleted-sender")
    recipient = await make_profile("accept-deleted-recipient")
    obj = await ready_object(owner=sender)

    share = await _offer_and_load(sender=sender, recipient=recipient, obj=obj)
    await object_service.delete_object(obj.id, owner=sender)

    with pytest.raises(ConflictError):
        await share_service.accept_share(owner=recipient, share_id=share.id)


async def test_accepting_with_no_destination_creates_shared_with_me_project():
    sender = await make_profile("accept-lazy-sender")
    recipient = await make_profile("accept-lazy-recipient")
    obj = await ready_object(owner=sender)

    share = await _offer_and_load(sender=sender, recipient=recipient, obj=obj)
    copy = await share_service.accept_share(owner=recipient, share_id=share.id)

    project = await project_service.get_project(copy.project_id, owner=recipient)
    assert project.name == share_service.SHARED_WITH_ME_PROJECT_NAME


async def test_accepting_twice_into_lazy_project_reuses_it():
    sender = await make_profile("accept-lazy2-sender")
    recipient = await make_profile("accept-lazy2-recipient")
    obj1 = await ready_object(owner=sender)
    obj2 = await ready_object(owner=sender)

    share1 = await _offer_and_load(sender=sender, recipient=recipient, obj=obj1)
    share2 = await _offer_and_load(sender=sender, recipient=recipient, obj=obj2)
    copy1 = await share_service.accept_share(owner=recipient, share_id=share1.id)
    copy2 = await share_service.accept_share(owner=recipient, share_id=share2.id)

    assert copy1.project_id == copy2.project_id
