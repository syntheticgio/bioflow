"""The profiles HTTP surface, and the owner partition proven through it.

Two different things are under test here and they are worth telling apart.

The first is the profiles router itself -- listing, creating, selecting,
deleting. Most of that is thin translation over `profile_service`, which has
its own tests; what these add is the wire contract the picker depends on, and
in particular that `password_hash` never crosses it.

The second is the reason the router exists at all: `TestOwnerPartition`
drives `/api/v1/projects` over HTTP with real profile headers and asserts a
project created under one profile is invisible to another. Every layer below
it is already tested in isolation, but only this test fails if the route
forgets to pass its resolved owner down -- which is precisely the mistake the
`TODO(profiles)` markers left lying around in a dozen route files.

The database is shared across the module (see `beanie_models`), so tests that
depend on the *global* profile count -- the last-profile refusal above all --
create their own isolated state and clean it up rather than assuming they run
alone.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.models import Profile
from app.services import profile_service, project_service

pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


class TestListProfiles:
    async def test_lists_created_profiles_sorted_by_username(self, client):
        await Profile.find_all().delete()
        await profile_service.create_profile(username="zoe")
        await profile_service.create_profile(username="adam")

        r = await client.get("/api/v1/profiles")

        assert r.status_code == 200
        assert [p["username"] for p in r.json()] == ["adam", "zoe"]

    async def test_an_installation_with_no_profiles_lists_nothing(self, client):
        """The state the picker starts from on a fresh install -- it must be an
        empty list and not an error, because this is the one call the frontend
        makes *before* it has a profile to send."""
        await Profile.find_all().delete()

        r = await client.get("/api/v1/profiles")

        assert r.status_code == 200
        assert r.json() == []

    async def test_the_password_hash_never_crosses_the_wire(self, client):
        """The picker needs to know a profile *has* a password so it can show
        the prompt; it must never receive the hash itself. Asserting on the
        serialized body rather than the parsed dict catches a hash that leaks
        under some other key name."""
        await Profile.find_all().delete()
        await profile_service.create_profile(username="secretive", password="hunter2")

        r = await client.get("/api/v1/profiles")

        assert "password_hash" not in r.text
        assert "hunter2" not in r.text
        body = r.json()
        assert len(body) == 1
        assert body[0]["has_password"] is True
        assert not any("$" in str(v) for v in body[0].values())

    async def test_a_profile_without_a_password_says_so(self, client):
        await Profile.find_all().delete()
        await profile_service.create_profile(username="open")

        r = await client.get("/api/v1/profiles")

        assert r.json()[0]["has_password"] is False

    async def test_listing_works_with_no_profile_header(self, client):
        """This endpoint is what you call before you have a profile, so wiring
        `OwnerDep` into it would deadlock the picker: no profile can be chosen
        because choosing one requires having chosen one."""
        await Profile.find_all().delete()
        await profile_service.create_profile(username="header-free")

        r = await client.get("/api/v1/profiles")

        assert r.status_code == 200
        assert [p["username"] for p in r.json()] == ["header-free"]


class TestCreateProfile:
    async def test_creates_a_profile_and_returns_it(self, client):
        await Profile.find_all().delete()

        r = await client.post("/api/v1/profiles", json={"username": "newcomer"})

        assert r.status_code == 201
        body = r.json()
        assert body["username"] == "newcomer"
        assert body["has_password"] is False
        assert "password_hash" not in body
        assert await Profile.get(body["id"]) is not None

    async def test_a_created_profile_appears_in_the_listing(self, client):
        await Profile.find_all().delete()

        await client.post("/api/v1/profiles", json={"username": "listed"})
        r = await client.get("/api/v1/profiles")

        assert [p["username"] for p in r.json()] == ["listed"]

    async def test_first_boot_adopts_the_legacy_owner(self, client):
        await Profile.find_all().delete()

        r = await client.post(
            "/api/v1/profiles", json={"username": "founder", "is_first_boot": True}
        )

        assert r.status_code == 201
        assert r.json()["adopted_legacy_owner"] is True

    async def test_a_second_first_boot_is_rejected(self, client):
        """`is_first_boot` is a claim the caller makes, not a fact -- a stale
        setup tab can send it again, and a second adopter would hand an
        existing library to whoever asked last."""
        await Profile.find_all().delete()
        await client.post(
            "/api/v1/profiles", json={"username": "first", "is_first_boot": True}
        )

        r = await client.post(
            "/api/v1/profiles", json={"username": "second", "is_first_boot": True}
        )

        assert r.status_code == 422
        assert r.json()["code"] == "validation_error"

    async def test_a_duplicate_username_is_a_conflict(self, client):
        await Profile.find_all().delete()
        await client.post("/api/v1/profiles", json={"username": "twin"})

        r = await client.post("/api/v1/profiles", json={"username": "twin"})

        assert r.status_code == 409
        assert r.json()["code"] == "conflict"

    async def test_an_empty_username_is_rejected(self, client):
        await Profile.find_all().delete()

        r = await client.post("/api/v1/profiles", json={"username": "   "})

        assert r.status_code == 422
        assert r.json()["code"] == "validation_error"

    async def test_the_error_body_is_the_apperror_shape_not_fastapis(self, client):
        """`frontend/src/api/client.ts` reads `body.code` and `body.message`.
        An `HTTPException` here would return `{"detail": ...}` instead, which
        the client silently discards -- leaving the picker with a failure it
        cannot explain."""
        await Profile.find_all().delete()
        await client.post("/api/v1/profiles", json={"username": "shape"})

        body = (await client.post("/api/v1/profiles", json={"username": "shape"})).json()

        assert set(body) == {"code", "message", "details"}


class TestSelectProfile:
    async def test_selecting_a_profile_with_no_password_succeeds(self, client):
        await Profile.find_all().delete()
        profile = await profile_service.create_profile(username="unlocked")

        r = await client.post(f"/api/v1/profiles/{profile.id}/select", json={})

        assert r.status_code == 200
        assert r.json()["username"] == "unlocked"

    async def test_selecting_with_the_right_password_succeeds(self, client):
        await Profile.find_all().delete()
        profile = await profile_service.create_profile(username="locked", password="s3cret")

        r = await client.post(
            f"/api/v1/profiles/{profile.id}/select", json={"password": "s3cret"}
        )

        assert r.status_code == 200
        assert r.json()["username"] == "locked"

    async def test_selecting_with_the_wrong_password_is_rejected(self, client):
        await Profile.find_all().delete()
        profile = await profile_service.create_profile(username="locked-2", password="s3cret")

        r = await client.post(
            f"/api/v1/profiles/{profile.id}/select", json={"password": "wrong"}
        )

        assert r.status_code == 403
        assert r.json()["code"] == "wrong_profile_password"

    async def test_a_rejected_selection_does_not_touch_last_used_at(self, client):
        """`last_used_at` orders the picker by who actually got in. A failed
        attempt that bumped it would float a profile nobody entered to the
        top."""
        await Profile.find_all().delete()
        profile = await profile_service.create_profile(username="locked-3", password="s3cret")

        await client.post(f"/api/v1/profiles/{profile.id}/select", json={"password": "no"})

        assert (await Profile.get(profile.id)).last_used_at is None

    async def test_selection_records_last_used_at(self, client):
        """The model documents this field as written on selection, and select
        is the only thing that writes it -- resolving the header on an
        ordinary request must not, or it becomes an activity log rather than
        the picker's sort key."""
        await Profile.find_all().delete()
        profile = await profile_service.create_profile(username="stamped")
        assert profile.last_used_at is None

        r = await client.post(f"/api/v1/profiles/{profile.id}/select", json={})

        assert r.json()["last_used_at"] is not None
        assert (await Profile.get(profile.id)).last_used_at is not None

    async def test_an_ordinary_request_does_not_stamp_last_used_at(self, client):
        await Profile.find_all().delete()
        profile = await profile_service.create_profile(username="unstamped")

        await client.get(
            "/api/v1/projects", headers={"X-BioFlow-Profile": profile.owner_id()}
        )

        assert (await Profile.get(profile.id)).last_used_at is None

    async def test_selecting_an_unknown_profile_is_a_404(self, client):
        """A well-formed id naming nothing is a missing resource, and it is the
        expected steady-state failure -- a remembered id goes stale the moment
        that profile is deleted. The picker recovers from it, so it must not
        arrive looking like the malformed-id case below."""
        await Profile.find_all().delete()

        r = await client.post("/api/v1/profiles/000000000000000000000000/select", json={})

        assert r.status_code == 404
        assert r.json()["code"] == "not_found"

    async def test_a_malformed_profile_id_is_a_422_not_a_500(self, client):
        """Distinct from the stale-id case above: this one is a bad request,
        not a missing resource. Asserting the exact status rather than merely
        `< 500` -- an accidental 200 would otherwise pass."""
        r = await client.post("/api/v1/profiles/not-an-id/select", json={})

        assert r.status_code == 422
        assert r.json()["code"] == "validation_error"

    async def test_the_body_is_optional(self, client):
        """The picker posts nothing at all for a passwordless profile."""
        await Profile.find_all().delete()
        profile = await profile_service.create_profile(username="bodyless")

        r = await client.post(f"/api/v1/profiles/{profile.id}/select")

        assert r.status_code == 200


class TestDeleteProfile:
    """The three refusals, each asserted on its own `code`.

    A test asserting only the status or the exception type passes with its
    guard deleted, because a different branch raises the same 409 -- which is
    exactly what happened to the last-profile guard once already. The `code`
    is the thing that distinguishes them, and it is also what the picker
    branches on: two of these are permanent refusals to explain, one is
    actionable ("delete its projects first").
    """

    async def test_deletes_an_empty_non_last_profile(self, client):
        await Profile.find_all().delete()
        await profile_service.create_profile(username="keeper")
        spare = await profile_service.create_profile(username="spare")

        r = await client.delete(f"/api/v1/profiles/{spare.id}")

        assert r.status_code == 204
        assert await Profile.get(spare.id) is None

    async def test_refuses_the_last_profile_with_its_own_code(self, client):
        """Deliberately a non-adopted profile: the adopted-owner guard refuses
        that too, so an adopted `only` here would still 409 with this guard
        gone."""
        await Profile.find_all().delete()
        only = await profile_service.create_profile(username="only")

        assert only.adopted_legacy_owner is False
        r = await client.delete(f"/api/v1/profiles/{only.id}")

        assert r.status_code == 409
        assert r.json()["code"] == "last_profile"
        assert await Profile.get(only.id) is not None

    async def test_refuses_the_adopted_profile_with_its_own_code(self, client):
        """Owns nothing and is not the last, so it clears both other guards --
        and is still unrecoverable to delete, because nothing else answers for
        `owner: "local"` and no replacement can adopt it."""
        await Profile.find_all().delete()
        adopter = await profile_service.create_profile(username="adopter", is_first_boot=True)
        await profile_service.create_profile(username="bystander")

        r = await client.delete(f"/api/v1/profiles/{adopter.id}")

        assert r.status_code == 409
        assert r.json()["code"] == "adopted_legacy_owner"
        assert await Profile.get(adopter.id) is not None

    async def test_refuses_a_profile_that_still_owns_things_and_says_how_many(self, client):
        """The one actionable refusal, so the counts have to reach the UI."""
        await Profile.find_all().delete()
        await profile_service.create_profile(username="keeper-2")
        doomed = await profile_service.create_profile(username="doomed")
        await project_service.create_project(name="held", owner=doomed.owner_id())

        r = await client.delete(f"/api/v1/profiles/{doomed.id}")

        assert r.status_code == 409
        body = r.json()
        assert body["code"] == "profile_not_empty"
        assert body["details"]["projects"] == 1
        assert await Profile.get(doomed.id) is not None

    async def test_the_three_refusals_do_not_share_a_code(self, client):
        """Collapsing them into one opaque conflict is the failure this guards
        against: the picker's recovery differs per reason, and it can only
        branch on something stable."""
        await Profile.find_all().delete()
        adopter = await profile_service.create_profile(username="a-adopter", is_first_boot=True)
        owning = await profile_service.create_profile(username="a-owning")
        await project_service.create_project(name="held", owner=owning.owner_id())

        codes = {
            (await client.delete(f"/api/v1/profiles/{adopter.id}")).json()["code"],
            (await client.delete(f"/api/v1/profiles/{owning.id}")).json()["code"],
        }
        await Profile.find({"username": "a-adopter"}).delete()
        await project_service.delete_project(
            (await project_service.list_projects(owner=owning.owner_id()))[0].id,
            owner=owning.owner_id(),
        )
        codes.add((await client.delete(f"/api/v1/profiles/{owning.id}")).json()["code"])

        assert len(codes) == 3

    async def test_deleting_an_unknown_profile_is_rejected(self, client):
        await Profile.find_all().delete()
        await profile_service.create_profile(username="present")

        r = await client.delete("/api/v1/profiles/000000000000000000000000")

        assert r.status_code == 422


class TestOwnerPartition:
    """The end-to-end proof, driven over HTTP with real headers.

    This is the test the whole feature is for. Everything below it is already
    covered in isolation -- `get_current_owner` in tests/api/test_deps.py, the
    owner filter in the project service's own tests -- but neither notices a
    route that resolves an owner and then hands the service a hardcoded
    `"local"` anyway, which is the shape of the `TODO(profiles)` markers this
    replaces.
    """

    async def test_projects_are_rejected_without_a_profile_header(self, client):
        r = await client.get("/api/v1/projects")

        assert r.status_code == 400
        assert r.json()["code"] == "profile_unresolved"

    async def test_projects_are_rejected_with_an_unknown_profile_header(self, client):
        r = await client.get(
            "/api/v1/projects", headers={"X-BioFlow-Profile": "000000000000000000000000"}
        )

        assert r.status_code == 400

    async def test_projects_succeed_with_a_valid_profile_header(self, client):
        await Profile.find_all().delete()
        profile = await profile_service.create_profile(username="lister")

        r = await client.get(
            "/api/v1/projects", headers={"X-BioFlow-Profile": profile.owner_id()}
        )

        assert r.status_code == 200
        assert r.json() == []

    async def test_creating_a_project_requires_a_profile_header(self, client):
        r = await client.post("/api/v1/projects", json={"name": "headerless"})

        assert r.status_code == 400
        assert r.json()["code"] == "profile_unresolved"

    async def test_one_profiles_project_is_invisible_to_another(self, client):
        """The actual partition, proven end to end.

        Both halves matter. Profile A must see what it created -- otherwise a
        route that always returned nothing would pass the isolation half --
        and B must not see it, which fails the moment `list_projects` stops
        filtering on the owner the header resolved to.
        """
        await Profile.find_all().delete()
        alice = await profile_service.create_profile(username="alice")
        bob = await profile_service.create_profile(username="bob")
        a_headers = {"X-BioFlow-Profile": alice.owner_id()}
        b_headers = {"X-BioFlow-Profile": bob.owner_id()}

        created = await client.post(
            "/api/v1/projects", json={"name": "alices-work"}, headers=a_headers
        )
        assert created.status_code == 201

        a_seen = await client.get("/api/v1/projects", headers=a_headers)
        b_seen = await client.get("/api/v1/projects", headers=b_headers)

        assert [p["name"] for p in a_seen.json()] == ["alices-work"]
        assert b_seen.json() == []

    async def test_a_created_project_is_stored_under_the_headers_owner(self, client):
        """Creation must file the project under the resolved owner, not the
        `"local"` default. Without this, the isolation test above could pass
        for the wrong reason -- everything landing under `"local"` and neither
        profile seeing any of it."""
        await Profile.find_all().delete()
        carol = await profile_service.create_profile(username="carol")

        r = await client.post(
            "/api/v1/projects",
            json={"name": "carols-work"},
            headers={"X-BioFlow-Profile": carol.owner_id()},
        )

        stored = await project_service.get_project(r.json()["id"], owner=carol.owner_id())
        assert stored.owner == carol.owner_id()
