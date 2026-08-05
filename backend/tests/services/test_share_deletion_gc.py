"""Deletion and GC around a shared blob -- characterization, not new behaviour.

`detach_blob_from_object` and `gc_candidates` already do the right thing (#25);
this file exists because nothing proved it. Per the plan (#51, "Deletion and
GC"), every assertion here lands on the blob ledger, `gc_candidates()`, or the
*surviving* party's object -- never on the deleter, since a test that only
checks the deleter passes whether or not the refcount was right.
"""

from datetime import UTC, datetime, timedelta

import pytest

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


async def _share_and_accept(*, sender: str, recipient: str, obj: DataObject) -> DataObject:
    share = await share_service.offer_share(owner=sender, object_id=obj.id, to_profile_id=recipient)
    return await share_service.accept_share(owner=recipient, share_id=share.id)


async def test_sender_deleting_after_acceptance_leaves_recipient_intact():
    sender = await make_profile("gc-sender-deletes-sender")
    recipient = await make_profile("gc-sender-deletes-recipient")
    obj = await ready_object(owner=sender)
    digest = obj.blob_sha256

    copy = await _share_and_accept(sender=sender, recipient=recipient, obj=obj)

    await object_service.delete_object(obj.id, owner=sender)

    recipient_obj = await object_service.get_object(copy.id, owner=recipient)
    assert recipient_obj.status is ObjectStatus.READY

    blob = await Blob.get(digest)
    assert blob.ref_count == 1

    candidates = await blob_service.gc_candidates()
    assert digest not in {b.id for b in candidates}


async def test_recipient_deleting_instead_is_symmetric():
    sender = await make_profile("gc-recipient-deletes-sender")
    recipient = await make_profile("gc-recipient-deletes-recipient")
    obj = await ready_object(owner=sender)
    digest = obj.blob_sha256

    copy = await _share_and_accept(sender=sender, recipient=recipient, obj=obj)

    await object_service.delete_object(copy.id, owner=recipient)

    sender_obj = await object_service.get_object(obj.id, owner=sender)
    assert sender_obj.status is ObjectStatus.READY

    blob = await Blob.get(digest)
    assert blob.ref_count == 1

    candidates = await blob_service.gc_candidates()
    assert digest not in {b.id for b in candidates}


async def test_both_deleting_makes_the_blob_collectable_only_past_the_grace_window():
    sender = await make_profile("gc-both-delete-sender")
    recipient = await make_profile("gc-both-delete-recipient")
    obj = await ready_object(owner=sender)
    digest = obj.blob_sha256

    copy = await _share_and_accept(sender=sender, recipient=recipient, obj=obj)

    await object_service.delete_object(obj.id, owner=sender)
    await object_service.delete_object(copy.id, owner=recipient)

    blob = await Blob.get(digest)
    assert blob.ref_count == 0

    # Freshly decremented -- inside the grace window, not yet a candidate.
    candidates = await blob_service.gc_candidates()
    assert digest not in {b.id for b in candidates}

    # Backdate updated_at past GC_GRACE rather than monkeypatching the
    # constant, so this exercises the real comparison gc_candidates makes.
    # blob_service.get_db(), not a module-level import of get_db -- the
    # beanie_models fixture patches the *module's* bound name, not the
    # original app.db.client symbol (see its docstring).
    past = datetime.now(UTC) - blob_service.GC_GRACE - timedelta(minutes=1)
    await blob_service.get_db().blobs.update_one({"_id": digest}, {"$set": {"updated_at": past}})

    candidates = await blob_service.gc_candidates()
    assert digest in {b.id for b in candidates}


async def test_sidecar_cascade_survives_the_senders_delete():
    sender = await make_profile("gc-sidecar-sender")
    recipient = await make_profile("gc-sidecar-recipient")
    project = await project_service.create_project(name="gc-sidecar-src", owner=sender)
    bam = await ready_object(owner=sender, project=project)
    bai = await object_service.ingest_local_file(
        project_id=project.id,
        path=scratch_file(b"gc-index-bytes"),
        name=f"{bam.name}.bai",
        owner=sender,
        sidecar_of=bam.id,
        sidecar_role=SidecarRole.BAI,
    )
    bai_digest = bai.blob_sha256

    bam_copy = await _share_and_accept(sender=sender, recipient=recipient, obj=bam)

    # delete_object on the sender's BAM cascades to the sender's BAI too.
    await object_service.delete_object(bam.id, owner=sender)

    recipient_sidecars = await object_service.list_sidecars(bam_copy.id, owner=recipient)
    assert len(recipient_sidecars) == 1
    sidecar_copy = recipient_sidecars[0]
    assert sidecar_copy.status is ObjectStatus.READY
    assert sidecar_copy.sidecar_of == bam_copy.id

    blob = await Blob.get(bai_digest)
    assert blob.ref_count == 1
    candidates = await blob_service.gc_candidates()
    assert bai_digest not in {b.id for b in candidates}
