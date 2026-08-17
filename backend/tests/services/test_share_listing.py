"""share_service.list_inbox / list_outbox.

The mandatory negative: create shares in both directions between three
profiles and assert each listing only returns rows for its own direction. A
test with two profiles and one share passes on a query that ignores direction
entirely.
"""

import pytest
from app.models import ShareState
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


async def test_inbox_and_outbox_are_direction_scoped():
    a = await make_profile("list-a")
    b = await make_profile("list-b")
    c = await make_profile("list-c")

    obj_a = await ready_object(owner=a)
    obj_b = await ready_object(owner=b)
    obj_c = await ready_object(owner=c)

    # a -> b, b -> c, c -> a
    share_ab = await share_service.offer_share(owner=a, object_id=obj_a.id, to_profile_id=b)
    share_bc = await share_service.offer_share(owner=b, object_id=obj_b.id, to_profile_id=c)
    share_ca = await share_service.offer_share(owner=c, object_id=obj_c.id, to_profile_id=a)

    b_inbox = await share_service.list_inbox(owner=b)
    assert [s.id for s in b_inbox] == [share_ab.id]

    a_inbox = await share_service.list_inbox(owner=a)
    assert [s.id for s in a_inbox] == [share_ca.id]

    a_outbox = await share_service.list_outbox(owner=a)
    assert [s.id for s in a_outbox] == [share_ab.id]

    b_outbox = await share_service.list_outbox(owner=b)
    assert [s.id for s in b_outbox] == [share_bc.id]


async def test_outbox_includes_every_state_inbox_defaults_to_offered():
    a = await make_profile("list-state-a")
    b = await make_profile("list-state-b")
    obj = await ready_object(owner=a)

    share = await share_service.offer_share(owner=a, object_id=obj.id, to_profile_id=b)
    await share_service.decline_share(owner=b, share_id=share.id)

    assert await share_service.list_inbox(owner=b) == []
    assert [s.state for s in await share_service.list_outbox(owner=a)] == [ShareState.DECLINED]
