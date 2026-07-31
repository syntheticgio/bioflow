"""Profile creation, first-boot adoption, and deletion refusal.

The adoption tests are the load-bearing ones: they are what distinguishes a
design that rewrites nothing from one that quietly needs a migration across
`objects`, `projects`, `runs` and `jobs`. See
docs/superpowers/specs/2026-07-31-profiles-design.md, "First boot adopts
`local`".

Every test deletes the profiles it made, because the whole feature keys off
whether the collection is empty and the database here is module-scoped and
shared. A test that leaves a profile behind silently turns the *next* test's
"first" profile into a second one.
"""

import pytest
import pytest_asyncio
from pymongo.errors import DuplicateKeyError

from app.errors import ConflictError, ValidationError
from app.models import Profile
from app.services import profile_service, project_service

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _empty_profiles(beanie_models):
    """An empty Profile collection before and after each test in this file.

    `loop_scope="module"` to match `beanie_models`: a function-scoped async
    fixture runs on a different event loop than the module-scoped Motor client,
    and every await here fails with "attached to a different loop".
    """
    await Profile.find_all().delete()
    yield
    await Profile.find_all().delete()


class TestFirstBootAdoption:
    async def test_first_profile_adopts_the_legacy_owner(self):
        profile = await profile_service.create_profile(
            username="ada", is_first_boot=True
        )

        assert profile.adopted_legacy_owner is True
        assert profile.owner_id() == "local"

    async def test_the_adopted_profile_still_has_a_real_object_id(self):
        """`Profile(id="local", ...)` raises `Id must be of type
        PydanticObjectId` -- adoption is a stored flag, not a string primary
        key. If these two ever got conflated, the profile would be
        unaddressable by the id-based branch of `get_current_owner`."""
        profile = await profile_service.create_profile(
            username="ada-real-id", is_first_boot=True
        )

        assert str(profile.id) != "local"
        assert await Profile.get(profile.id) is not None

    async def test_pre_existing_documents_belong_to_the_adopted_profile(self):
        """The entire point of the design: zero documents rewritten.

        The project is created the way a pre-feature installation left it --
        carrying the `owner: "local"` default from TimestampedDocument -- and
        then a profile is created *afterwards* and can see it. Nothing
        migrates it; the profile moves to where the data already is.
        """
        legacy = await project_service.create_project(
            name="library-from-before-profiles", owner="local"
        )

        profile = await profile_service.create_profile(
            username="adopter", is_first_boot=True
        )

        visible = await project_service.list_projects(owner=profile.owner_id())
        assert legacy.id in [p.id for p in visible]

    async def test_a_second_profile_does_not_adopt(self):
        """Only one profile can own the pre-feature library, and it is the
        first. A second adopter would hand a whole existing library to someone
        who just made an account."""
        await profile_service.create_profile(username="first", is_first_boot=True)

        second = await profile_service.create_profile(username="second")

        assert second.adopted_legacy_owner is False
        assert second.owner_id() == str(second.id)

    async def test_first_boot_is_refused_once_a_profile_exists(self):
        """`is_first_boot` is a claim the caller makes, and the setup screen
        can be reached again by an old tab or a back button. The service
        checks the claim rather than trusting it."""
        await profile_service.create_profile(username="incumbent", is_first_boot=True)

        with pytest.raises(ValidationError):
            await profile_service.create_profile(
                username="latecomer", is_first_boot=True
            )

    async def test_the_database_refuses_a_second_adopter_outright(self):
        """The service's own check is a read-then-write and cannot survive a
        race: two concurrent setup requests can both see an empty collection
        before either inserts, and `uniq_username` does not catch that -- the
        racers have different usernames, so both inserts succeed.

        This inserts the second adopter directly, bypassing the service, which
        is what the losing racer effectively does. The
        `uniq_adopted_legacy_owner` partial index is what makes "at most one
        adopted profile" true regardless of timing; without it this test is
        the only thing that fails, and in production two profiles would
        quietly share one library with nothing to tell them apart.
        """
        await profile_service.create_profile(username="racer-one", is_first_boot=True)

        with pytest.raises(DuplicateKeyError):
            await Profile(
                username="racer-two", display={}, adopted_legacy_owner=True
            ).insert()

    async def test_the_partial_index_still_allows_many_non_adopters(self):
        """The index must be partial. A plain unique index on the flag would
        let only one profile in the entire installation have it False."""
        await profile_service.create_profile(username="ordinary-one")
        await profile_service.create_profile(username="ordinary-two")

        assert await Profile.find_all().count() == 2


class TestCreateProfile:
    async def test_empty_username_is_rejected(self):
        with pytest.raises(ValidationError):
            await profile_service.create_profile(username="   ")

    async def test_username_is_stripped(self):
        profile = await profile_service.create_profile(username="  spaced  ")

        assert profile.username == "spaced"

    async def test_duplicate_username_is_a_conflict(self):
        await profile_service.create_profile(username="twin")

        with pytest.raises(ConflictError):
            await profile_service.create_profile(username="twin")

    async def test_password_round_trips(self):
        profile = await profile_service.create_profile(
            username="with-password", password="hunter2"
        )

        assert profile.password_hash is not None
        assert profile.password_hash != "hunter2"
        assert profile_service.verify_password(profile, "hunter2") is True
        assert profile_service.verify_password(profile, "hunter3") is False

    async def test_no_password_means_no_hash_and_anything_verifies(self):
        """A profile with no password is not locked -- it is the default, and
        the picker opens it on a click. Verification has to say yes, or the
        speed bump becomes a wall for everyone who never set one."""
        profile = await profile_service.create_profile(username="open-door")

        assert profile.password_hash is None
        assert profile_service.verify_password(profile, "") is True
        assert profile_service.verify_password(profile, "whatever") is True

    async def test_two_profiles_with_the_same_password_get_different_hashes(self):
        """Salted, so a glance at the collection does not reveal that two
        people picked the same word."""
        a = await profile_service.create_profile(username="salt-a", password="same")
        b = await profile_service.create_profile(username="salt-b", password="same")

        assert a.password_hash != b.password_hash


class TestCountOwnedDocuments:
    async def test_counts_are_read_under_owner_id_not_the_raw_id(self):
        """The adopted profile's documents are filed under "local"; counting
        by `str(profile.id)` would report zero and let a deletion through that
        strands the entire pre-feature library."""
        await project_service.create_project(name="counted-legacy", owner="local")
        profile = await profile_service.create_profile(
            username="counter", is_first_boot=True
        )

        counts = await profile_service.count_owned_documents(profile.owner_id())

        assert counts["projects"] >= 1


class TestDeleteProfile:
    async def test_refuses_a_profile_that_still_owns_projects(self):
        keeper = await profile_service.create_profile(username="keeper")
        doomed = await profile_service.create_profile(username="doomed")
        await project_service.create_project(name="held", owner=doomed.owner_id())

        with pytest.raises(ConflictError) as exc_info:
            await profile_service.delete_profile(doomed.id)

        assert exc_info.value.details["projects"] == 1
        assert "doomed" in exc_info.value.message
        assert await Profile.get(doomed.id) is not None
        assert await Profile.get(keeper.id) is not None

    async def test_deletes_an_empty_non_last_profile(self):
        await profile_service.create_profile(username="survivor")
        spare = await profile_service.create_profile(username="spare")

        await profile_service.delete_profile(spare.id)

        assert await Profile.get(spare.id) is None

    async def test_refuses_the_last_profile_even_when_empty(self):
        """Deleting the last profile drops the installation into the
        first-boot setup screen, and a setup screen on an installation that
        already has blobs on disk is a state nothing else in the app is
        designed for."""
        only = await profile_service.create_profile(username="only", is_first_boot=True)

        with pytest.raises(ConflictError):
            await profile_service.delete_profile(only.id)

        assert await Profile.get(only.id) is not None

    async def test_unknown_profile_is_a_validation_error(self):
        await profile_service.create_profile(username="bystander")

        with pytest.raises(ValidationError):
            await profile_service.delete_profile("000000000000000000000000")
