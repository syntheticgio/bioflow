import pytest

from app.api.deps import get_current_owner
from app.errors import ProfileUnresolvedError
from app.models import Profile

pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


class TestGetCurrentOwner:
    async def test_resolves_a_known_profile_header_to_its_owner_id(self):
        profile = await Profile(username="deps-known", display={}).insert()

        owner = await get_current_owner(x_bioflow_profile=str(profile.id))

        assert owner == str(profile.id)

    async def test_missing_header_is_profile_unresolved(self):
        with pytest.raises(ProfileUnresolvedError):
            await get_current_owner(x_bioflow_profile=None)

    async def test_unknown_profile_id_is_profile_unresolved(self):
        """One code, not three. The picker's recovery branch is "this id is no
        good, show the picker again"; it should not have to tell a well-formed
        id naming a deleted profile apart from a garbled one to take it."""
        with pytest.raises(ProfileUnresolvedError) as exc_info:
            await get_current_owner(x_bioflow_profile="000000000000000000000000")

        assert exc_info.value.code == "profile_unresolved"

    async def test_resolution_goes_through_owner_id_not_str_id(self):
        """The returned owner must come from `Profile.owner_id()`.

        The two are the same string today, so a `str(profile.id)` here would
        pass every other test in this file -- and then quietly ignore the
        adoption branch that `owner_id()` grows later, handing back a real
        ObjectId for the one profile whose documents are keyed "local"."""
        profile = await Profile(username="deps-via-owner-id", display={}).insert()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(Profile, "owner_id", lambda self: "sentinel-owner")

            owner = await get_current_owner(x_bioflow_profile=str(profile.id))

        assert owner == "sentinel-owner"

    async def test_local_header_resolves_without_matching_on_username(self):
        """A "local" header must not be resolved by looking for a profile
        *named* "local" -- the adopted profile is identified by a flag a later
        task adds, and may be named anything. Only profiles with other names
        exist here, so a username match would 404."""
        await Profile(username="deps-not-called-local", display={}).insert()

        assert await get_current_owner(x_bioflow_profile="local") == "local"

    async def test_malformed_profile_id_raises_400_not_500(self):
        """`PydanticObjectId("not-an-id")` raises bson's InvalidId, which is a
        BSONError and *not* a ValueError -- catching the wrong type here would
        let a typo'd header escape as an unhandled 500 rather than a 400."""
        with pytest.raises(ProfileUnresolvedError):
            await get_current_owner(x_bioflow_profile="not-an-id")

    async def test_local_header_against_an_empty_database_is_rejected(self):
        """The one case the `find_one()` existence check exists for.

        Every other rejection test here goes through the ObjectId branch or
        inserts a profile first, so deleting that check would leave them all
        green while `"local"` silently resolved on an installation that has no
        profiles at all -- handing back an owner string for a library nobody
        owns. The deletion is undone so sibling tests, which share a
        module-scoped database, still see the profiles they inserted."""
        existing = await Profile.find_all().to_list()
        await Profile.find_all().delete()
        try:
            with pytest.raises(ProfileUnresolvedError):
                await get_current_owner(x_bioflow_profile="local")
        finally:
            for profile in existing:
                await profile.insert()
