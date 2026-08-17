"""share_service.offer_share.

Per the profiles design's testing discipline, the tests that matter are the
refusals: creating an object as A and offering it as A passes whether or not
the ownership check exists, so the meaningful assertion is that the WRONG
owner is refused.
"""

import pytest
from app.errors import ConflictError, NotFoundError, ProfileUnresolvedError, ValidationError
from app.models import ObjectStatus
from app.services import object_service, share_service
from beanie import PydanticObjectId

from tests.services.helpers_share import make_profile, ready_object

pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


@pytest.fixture(autouse=True)
def _no_queue(monkeypatch):
    """Same stub test_object_service_owner.py uses: ingest_local_file enqueues
    a header-parse job that needs live Redis, orthogonal to sharing."""

    async def _skip(obj, **kwargs):
        return ""

    monkeypatch.setattr(object_service, "enqueue_ingest", _skip)


@pytest.fixture(autouse=True)
def _no_events(monkeypatch):
    """publish_event needs live Redis; sharing's own behavior does not depend
    on the publish succeeding (it is fire-and-forget in queue.py already)."""
    from app.queue import queue

    async def _skip(*args, **kwargs):
        return None

    monkeypatch.setattr(queue, "publish_event", _skip)


@pytest.fixture(autouse=True, scope="module")
def _cleanup_scratch():
    yield
    from tests.services.helpers_share import reclaim_scratch_files

    reclaim_scratch_files()


async def test_offer_records_a_denormalized_snapshot():
    sender = await make_profile("offer-sender-a")
    recipient = await make_profile("offer-recipient-a")
    obj = await ready_object(owner=sender)

    share = await share_service.offer_share(
        owner=sender, object_id=obj.id, to_profile_id=recipient
    )

    assert share.from_owner == sender
    assert share.to_owner == recipient
    assert share.name == obj.name
    assert share.size == obj.size
    assert share.blob_sha256 == obj.blob_sha256


async def test_offering_an_object_you_do_not_own_raises_not_found():
    owner_a = await make_profile("offer-owner-a")
    owner_b = await make_profile("offer-owner-b")
    recipient = await make_profile("offer-owner-recipient")
    obj = await ready_object(owner=owner_a)

    with pytest.raises(NotFoundError):
        await share_service.offer_share(
            owner=owner_b, object_id=obj.id, to_profile_id=recipient
        )


async def test_offering_to_yourself_is_rejected():
    owner = await make_profile("offer-self")
    obj = await ready_object(owner=owner)

    with pytest.raises(ValidationError):
        await share_service.offer_share(owner=owner, object_id=obj.id, to_profile_id=owner)


async def test_offering_to_an_unknown_profile_is_rejected():
    owner = await make_profile("offer-unknown-target")
    obj = await ready_object(owner=owner)

    with pytest.raises(ProfileUnresolvedError):
        await share_service.offer_share(
            owner=owner,
            object_id=obj.id,
            to_profile_id=str(PydanticObjectId()),
        )


async def test_offering_a_non_ready_object_is_rejected():
    owner = await make_profile("offer-not-ready")
    recipient = await make_profile("offer-not-ready-recipient")
    obj = await ready_object(owner=owner)
    await obj.set({"status": ObjectStatus.ERROR})

    with pytest.raises(ConflictError):
        await share_service.offer_share(
            owner=owner, object_id=obj.id, to_profile_id=recipient
        )


async def test_a_second_pending_offer_of_the_same_object_conflicts():
    owner = await make_profile("offer-dup-sender")
    recipient = await make_profile("offer-dup-recipient")
    obj = await ready_object(owner=owner)

    await share_service.offer_share(owner=owner, object_id=obj.id, to_profile_id=recipient)

    with pytest.raises(ConflictError):
        await share_service.offer_share(
            owner=owner, object_id=obj.id, to_profile_id=recipient
        )
