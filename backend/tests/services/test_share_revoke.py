"""share_service.decline_share / revoke_share.

`test_revoking_an_accepted_share_leaves_the_recipient_object_intact` is the
one that encodes the policy decision (design note, "Offer, accept, decline,
revoke"): assert both that the call raises AND that the recipient's object and
the blob's refcount are unchanged -- a raise that happens after a partial
delete would pass the first half alone.
"""

import pytest
from app.errors import ConflictError, NotFoundError
from app.models import Blob, ShareState
from app.services import object_service, share_service

from tests.services.helpers_share import make_profile, ready_object

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


async def test_revoking_a_pending_offer_withdraws_it():
    sender = await make_profile("revoke-pending-sender")
    recipient = await make_profile("revoke-pending-recipient")
    obj = await ready_object(owner=sender)
    share = await share_service.offer_share(
        owner=sender, object_id=obj.id, to_profile_id=recipient
    )

    revoked = await share_service.revoke_share(owner=sender, share_id=share.id)

    assert revoked.state is ShareState.WITHDRAWN
    assert await share_service.list_inbox(owner=recipient) == []


async def test_revoking_an_accepted_share_is_refused():
    sender = await make_profile("revoke-accepted-sender")
    recipient = await make_profile("revoke-accepted-recipient")
    obj = await ready_object(owner=sender)
    share = await share_service.offer_share(
        owner=sender, object_id=obj.id, to_profile_id=recipient
    )
    await share_service.accept_share(owner=recipient, share_id=share.id)

    with pytest.raises(ConflictError):
        await share_service.revoke_share(owner=sender, share_id=share.id)


async def test_revoking_an_accepted_share_leaves_the_recipient_object_intact():
    sender = await make_profile("revoke-intact-sender")
    recipient = await make_profile("revoke-intact-recipient")
    obj = await ready_object(owner=sender)
    share = await share_service.offer_share(
        owner=sender, object_id=obj.id, to_profile_id=recipient
    )
    copy = await share_service.accept_share(owner=recipient, share_id=share.id)
    blob_before = await Blob.get(obj.blob_sha256)

    with pytest.raises(ConflictError):
        await share_service.revoke_share(owner=sender, share_id=share.id)

    still_there = await object_service.get_object(copy.id, owner=recipient)
    assert still_there.id == copy.id
    blob_after = await Blob.get(obj.blob_sha256)
    assert blob_after.ref_count == blob_before.ref_count


async def test_only_the_sender_can_revoke():
    sender = await make_profile("revoke-authz-sender")
    recipient = await make_profile("revoke-authz-recipient")
    stranger = await make_profile("revoke-authz-stranger")
    obj = await ready_object(owner=sender)
    share = await share_service.offer_share(
        owner=sender, object_id=obj.id, to_profile_id=recipient
    )

    with pytest.raises(NotFoundError):
        await share_service.revoke_share(owner=stranger, share_id=share.id)
    with pytest.raises(NotFoundError):
        await share_service.revoke_share(owner=recipient, share_id=share.id)


async def test_only_the_recipient_can_decline():
    sender = await make_profile("decline-authz-sender")
    recipient = await make_profile("decline-authz-recipient")
    stranger = await make_profile("decline-authz-stranger")
    obj = await ready_object(owner=sender)
    share = await share_service.offer_share(
        owner=sender, object_id=obj.id, to_profile_id=recipient
    )

    with pytest.raises(NotFoundError):
        await share_service.decline_share(owner=stranger, share_id=share.id)
    with pytest.raises(NotFoundError):
        await share_service.decline_share(owner=sender, share_id=share.id)


async def test_a_declined_offer_can_be_re_offered():
    sender = await make_profile("decline-reoffer-sender")
    recipient = await make_profile("decline-reoffer-recipient")
    obj = await ready_object(owner=sender)
    first = await share_service.offer_share(
        owner=sender, object_id=obj.id, to_profile_id=recipient
    )
    await share_service.decline_share(owner=recipient, share_id=first.id)

    second = await share_service.offer_share(
        owner=sender, object_id=obj.id, to_profile_id=recipient
    )
    assert second.id != first.id
    assert second.state is ShareState.OFFERED
