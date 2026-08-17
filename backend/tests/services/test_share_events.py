"""offer_share publishes to the RECIPIENT's channel.

The natural typo is publishing to the sender's own channel (they are the
caller, after all) -- which produces a notification that reaches everyone
except the person who needs it. /events is already partitioned per profile, so
this needs no new plumbing, only the right owner argument.
"""

import pytest
from app.queue import queue
from app.services import object_service, share_service

from tests.services.helpers_share import make_profile, ready_object

pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


@pytest.fixture(autouse=True)
def _no_queue(monkeypatch):
    async def _skip(obj, **kwargs):
        return ""

    monkeypatch.setattr(object_service, "enqueue_ingest", _skip)


@pytest.fixture(autouse=True, scope="module")
def _cleanup_scratch():
    yield
    from tests.services.helpers_share import reclaim_scratch_files

    reclaim_scratch_files()


async def test_offer_publishes_to_the_recipients_channel(monkeypatch):
    sender = await make_profile("events-sender")
    recipient = await make_profile("events-recipient")
    obj = await ready_object(owner=sender)

    calls = []

    async def _capture(event_type, data, *, owner):
        calls.append((event_type, owner))

    monkeypatch.setattr(queue, "publish_event", _capture)

    await share_service.offer_share(owner=sender, object_id=obj.id, to_profile_id=recipient)

    assert calls == [("share.offered", recipient)]


async def test_accept_publishes_to_the_senders_channel(monkeypatch):
    """The opposite direction from offer. Publishing to the acceptor's own
    channel would notify the one person who already knows what happened --
    the sender is the one left wondering whether the accept even landed."""
    sender = await make_profile("events-accept-sender")
    recipient = await make_profile("events-accept-recipient")
    obj = await ready_object(owner=sender)
    share = await share_service.offer_share(
        owner=sender, object_id=obj.id, to_profile_id=recipient
    )

    calls = []

    async def _capture(event_type, data, *, owner):
        calls.append((event_type, owner))

    monkeypatch.setattr(queue, "publish_event", _capture)

    await share_service.accept_share(owner=recipient, share_id=share.id)

    assert calls == [("share.accepted", sender)]


async def test_decline_publishes_to_the_senders_channel(monkeypatch):
    sender = await make_profile("events-decline-sender")
    recipient = await make_profile("events-decline-recipient")
    obj = await ready_object(owner=sender)
    share = await share_service.offer_share(
        owner=sender, object_id=obj.id, to_profile_id=recipient
    )

    calls = []

    async def _capture(event_type, data, *, owner):
        calls.append((event_type, owner))

    monkeypatch.setattr(queue, "publish_event", _capture)

    await share_service.decline_share(owner=recipient, share_id=share.id)

    assert calls == [("share.declined", sender)]
