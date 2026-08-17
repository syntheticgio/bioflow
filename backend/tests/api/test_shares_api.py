"""The shares HTTP surface: status codes and the owner-header requirement.

Every route here takes `OwnerDep`, unlike `profiles.py` -- there is always a
caller with a profile by the time sharing happens. That is the one thing this
file adds beyond share_service's own tests, which already cover the policy in
depth: a missing/malformed header must 400 here exactly as it does on every
other partitioned router (`test_route_owner_scoping.py`).
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services import object_service, profile_service
from tests.services.helpers_share import ready_object, reclaim_scratch_files

pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


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
    reclaim_scratch_files()


async def _ready_object(owner: str):
    return await ready_object(owner=owner)


async def test_offer_requires_a_profile_header(client):
    r = await client.post(
        "/api/v1/shares",
        json={"object_id": "000000000000000000000000", "to_profile_id": "x"},
    )

    assert r.status_code == 400
    assert r.json()["code"] == "profile_unresolved"


async def test_offer_succeeds_and_appears_in_the_recipients_inbox(client, two_profiles):
    a, b = two_profiles["a"], two_profiles["b"]
    obj = await _ready_object(a.owner_id())

    r = await client.post(
        "/api/v1/shares",
        json={"object_id": str(obj.id), "to_profile_id": b.owner_id()},
        headers=two_profiles["a_headers"],
    )

    assert r.status_code == 201
    body = r.json()
    assert body["state"] == "offered"
    assert body["name"] == obj.name

    inbox = await client.get("/api/v1/shares/inbox", headers=two_profiles["b_headers"])
    assert [s["id"] for s in inbox.json()] == [body["id"]]


async def test_offering_someone_elses_object_is_a_404(client, two_profiles):
    a = two_profiles["a"]
    obj = await _ready_object(a.owner_id())

    r = await client.post(
        "/api/v1/shares",
        json={"object_id": str(obj.id), "to_profile_id": a.owner_id()},
        headers=two_profiles["b_headers"],
    )

    assert r.status_code == 404


async def test_a_duplicate_pending_offer_is_a_409(client, two_profiles):
    a, b = two_profiles["a"], two_profiles["b"]
    obj = await _ready_object(a.owner_id())
    body = {"object_id": str(obj.id), "to_profile_id": b.owner_id()}

    first = await client.post("/api/v1/shares", json=body, headers=two_profiles["a_headers"])
    assert first.status_code == 201

    second = await client.post("/api/v1/shares", json=body, headers=two_profiles["a_headers"])
    assert second.status_code == 409


async def test_accept_materializes_the_object_for_the_recipient(client, two_profiles):
    a, b = two_profiles["a"], two_profiles["b"]
    obj = await _ready_object(a.owner_id())
    offered = await client.post(
        "/api/v1/shares",
        json={"object_id": str(obj.id), "to_profile_id": b.owner_id()},
        headers=two_profiles["a_headers"],
    )
    share_id = offered.json()["id"]

    r = await client.post(
        f"/api/v1/shares/{share_id}/accept", json={}, headers=two_profiles["b_headers"]
    )

    assert r.status_code == 200
    assert r.json()["state"] == "accepted"
    assert r.json()["accepted_object_id"] is not None


async def test_decline_requires_the_recipient(client, two_profiles):
    a, b = two_profiles["a"], two_profiles["b"]
    obj = await _ready_object(a.owner_id())
    offered = await client.post(
        "/api/v1/shares",
        json={"object_id": str(obj.id), "to_profile_id": b.owner_id()},
        headers=two_profiles["a_headers"],
    )
    share_id = offered.json()["id"]

    wrong = await client.post(
        f"/api/v1/shares/{share_id}/decline", headers=two_profiles["a_headers"]
    )
    assert wrong.status_code == 404

    right = await client.post(
        f"/api/v1/shares/{share_id}/decline", headers=two_profiles["b_headers"]
    )
    assert right.status_code == 200
    assert right.json()["state"] == "declined"


async def test_revoking_an_accepted_share_is_a_409(client, two_profiles):
    a, b = two_profiles["a"], two_profiles["b"]
    obj = await _ready_object(a.owner_id())
    offered = await client.post(
        "/api/v1/shares",
        json={"object_id": str(obj.id), "to_profile_id": b.owner_id()},
        headers=two_profiles["a_headers"],
    )
    share_id = offered.json()["id"]
    await client.post(
        f"/api/v1/shares/{share_id}/accept", json={}, headers=two_profiles["b_headers"]
    )

    r = await client.delete(f"/api/v1/shares/{share_id}", headers=two_profiles["a_headers"])

    assert r.status_code == 409


async def test_revoking_a_pending_offer_succeeds(client, two_profiles):
    a, b = two_profiles["a"], two_profiles["b"]
    obj = await _ready_object(a.owner_id())
    offered = await client.post(
        "/api/v1/shares",
        json={"object_id": str(obj.id), "to_profile_id": b.owner_id()},
        headers=two_profiles["a_headers"],
    )
    share_id = offered.json()["id"]

    r = await client.delete(f"/api/v1/shares/{share_id}", headers=two_profiles["a_headers"])

    assert r.status_code == 200
    assert r.json()["state"] == "withdrawn"


async def test_share_out_names_the_other_profile(client, two_profiles):
    """The inbox has to say who, and the raw owner string cannot answer that."""
    a, b = two_profiles["a"], two_profiles["b"]
    obj = await _ready_object(a.owner_id())

    r = await client.post(
        "/api/v1/shares",
        json={"object_id": str(obj.id), "to_profile_id": b.owner_id()},
        headers=two_profiles["a_headers"],
    )

    body = r.json()
    assert body["from_profile"]["username"] == a.username
    assert body["from_profile"]["emoji"] == a.display.emoji
    assert body["to_profile"]["username"] == b.username


async def test_share_out_resolves_the_adopted_profile(client):
    """The regression this task exists for. The adopted profile's owner string
    is the literal "local", which matches no profile id -- so a resolver keyed
    on `str(profile.id)` returns nothing for exactly the profile holding the
    pre-existing library."""
    adopter = await profile_service.create_profile(username="sh-adopter", is_first_boot=True)
    other = await profile_service.create_profile(username="sh-other")
    obj = await _ready_object(adopter.owner_id())  # owner is "local"
    assert adopter.owner_id() == "local"

    r = await client.post(
        "/api/v1/shares",
        json={"object_id": str(obj.id), "to_profile_id": other.owner_id()},
        headers={"X-BioFlow-Profile": adopter.owner_id()},
    )

    assert r.json()["from_profile"]["username"] == "sh-adopter"

    await adopter.delete()
    await other.delete()
