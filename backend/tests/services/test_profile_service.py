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
from app.errors import ConflictError, ValidationError
from app.models import DataObject, Profile, Project, Share, ShareState
from app.services import object_service, profile_service, project_service, share_service
from pymongo.errors import DuplicateKeyError

from tests.services.helpers_share import ready_object

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


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


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _empty_profiles(beanie_models):
    """An empty Profile collection *and* an empty library before and after each
    test in this file.

    Clearing profiles alone is not enough, and the gap was live: several tests
    create projects at `owner="local"` to stand in for a pre-feature library
    and leave them behind. A later test whose profile adopts `"local"`
    inherits them, so `test_refuses_the_last_profile_even_when_empty` was not
    running against an empty profile at all -- it passed with its own guard
    deleted, because the *documents* branch raised the ConflictError it was
    catching. Anything owner-scoped this file creates has to go with it.

    `loop_scope="module"` to match `beanie_models`: a function-scoped async
    fixture runs on a different event loop than the module-scoped Motor client,
    and every await here fails with "attached to a different loop".
    """
    await _clear()
    yield
    await _clear()


async def _clear():
    await Profile.find_all().delete()
    await Project.find_all().delete()
    await DataObject.find_all().delete()
    await Share.find_all().delete()


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

    async def test_a_non_ascii_stored_hash_verifies_false_rather_than_raising(self):
        """`_hash_password` only ever writes a hexdigest, so this needs a
        hand-edited or corrupted document to reach -- but `compare_digest`
        raises TypeError on non-ASCII `str`, and every other malformed shape
        of `password_hash` already returns False rather than exploding."""
        profile = await profile_service.create_profile(username="corrupted")
        profile.password_hash = "salt$dígest"

        assert profile_service.verify_password(profile, "anything") is False

    async def test_two_profiles_with_the_same_password_get_different_hashes(self):
        """Salted, so a glance at the collection does not reveal that two
        people picked the same word."""
        a = await profile_service.create_profile(username="salt-a", password="same")
        b = await profile_service.create_profile(username="salt-b", password="same")

        assert a.password_hash != b.password_hash


class TestCountOwnedDocuments:
    async def test_delete_counts_the_adopted_profiles_documents_under_local(self):
        """`delete_profile` must count by `owner_id()`, never
        `str(profile.id)`.

        This exercises `delete_profile` rather than calling
        `count_owned_documents` itself -- calling it directly with
        `profile.owner_id()` only re-asserts that `owner_id()` works and can
        never observe what the deletion path passes, which is the thing at
        risk.

        The adopted profile is the *only* profile where the two differ: for
        every other one `owner_id()` is `str(id)` and the mutation is
        invisible. So the assertion is on the refusal's contents, not merely
        that it refuses. Counting by the ObjectId finds zero documents, and
        the deletion falls through to the adopted-owner guard -- a
        ConflictError still, but one carrying no `projects` count. Asserting
        the real count is what distinguishes them.
        """
        await project_service.create_project(name="counted-legacy", owner="local")
        adopter = await profile_service.create_profile(
            username="counter", is_first_boot=True
        )
        # Second profile so the last-profile guard does not fire first and
        # refuse for an unrelated reason.
        await profile_service.create_profile(username="bystander")

        with pytest.raises(ConflictError) as exc_info:
            await profile_service.delete_profile(adopter.id)

        assert exc_info.value.details["projects"] == 1
        assert "counter" in exc_info.value.message


class TestDeleteProfile:
    async def test_refuses_a_profile_that_still_owns_projects(self):
        keeper = await profile_service.create_profile(username="keeper")
        doomed = await profile_service.create_profile(username="doomed")
        await project_service.create_project(name="held", owner=doomed.owner_id())

        with pytest.raises(ConflictError) as exc_info:
            await profile_service.delete_profile(doomed.id)

        assert exc_info.value.code == "profile_not_empty"
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
        designed for.

        Two things here are load-bearing against a test that passes with its
        own guard deleted. The profile must not be the adopted one -- the
        adopted-owner guard refuses that too, so an adopted `only` would still
        raise ConflictError with this guard gone. Hence a spare profile
        created and deleted to leave a *non-adopted* last profile behind.
        And the assertion names this guard's `code` rather than accepting any
        ConflictError, since three separate branches raise that type.
        """
        only = await profile_service.create_profile(username="only")
        spare = await profile_service.create_profile(username="spare-to-remove")
        await profile_service.delete_profile(spare.id)

        assert only.adopted_legacy_owner is False
        assert await Profile.find_all().count() == 1

        with pytest.raises(ConflictError) as exc_info:
            await profile_service.delete_profile(only.id)

        assert exc_info.value.code == "last_profile"
        assert await Profile.get(only.id) is not None

    async def test_refuses_the_adopted_profile_even_when_it_owns_nothing(self):
        """The one deletion that has no recovery path.

        An adopted profile owning no projects or objects but not the last one
        would sail through both other guards -- and it is reachable, because
        `owner` also lives on runs, jobs, upload sessions and schedules, which
        the count does not see. An installation that deleted its projects but
        kept its run history counts zero here.

        What makes it unrecoverable rather than merely bad: nothing else
        carries `adopted_legacy_owner=True` afterwards, so `get_current_owner`
        raises for every `"local"` header from then on, and no new profile can
        take over because `create_profile` refuses `is_first_boot` once any
        profile exists. The pre-feature library becomes unreachable with no
        message and no way back.
        """
        adopter = await profile_service.create_profile(
            username="adopter", is_first_boot=True
        )
        await profile_service.create_profile(username="latecomer")

        assert await profile_service.count_owned_documents(adopter.owner_id()) == {
            "projects": 0,
            "objects": 0,
        }

        with pytest.raises(ConflictError) as exc_info:
            await profile_service.delete_profile(adopter.id)

        assert exc_info.value.code == "adopted_legacy_owner"
        assert await Profile.get(adopter.id) is not None
        assert await Profile.find_one({"adopted_legacy_owner": True}) is not None

    async def test_unknown_profile_is_a_validation_error(self):
        await profile_service.create_profile(username="bystander")

        with pytest.raises(ValidationError):
            await profile_service.delete_profile("000000000000000000000000")

    async def test_malformed_profile_id_is_a_validation_error(self):
        """Beanie validates the id before querying, so this raises pydantic's
        ValidationError -- which has no handler registered, and would surface
        as an unhandled 500 from a DELETE route rather than a 422."""
        await profile_service.create_profile(username="onlooker")

        with pytest.raises(ValidationError):
            await profile_service.delete_profile("not-an-object-id")


class TestDeleteProfileSharesCleanup:
    """#51: deleting a profile must not strand the inbox or outbox on the
    other side. Pending offers naming this profile on either side are
    deleted with it; ACCEPTED shares are kept, since the surviving profile's
    copied object's `shared_from.share_id` is the only record of where it
    came from."""

    async def test_deleting_the_sender_removes_a_pending_outgoing_offer(self):
        sender = await profile_service.create_profile(username="cleanup-out-sender")
        recipient = await profile_service.create_profile(username="cleanup-out-recipient")
        obj = await ready_object(owner=sender.owner_id())

        share = await share_service.offer_share(
            owner=sender.owner_id(), object_id=obj.id, to_profile_id=str(recipient.id)
        )
        await object_service.delete_object(obj.id, owner=sender.owner_id())
        await project_service.delete_project(obj.project_id, owner=sender.owner_id())

        await profile_service.delete_profile(sender.id)

        inbox = await share_service.list_inbox(owner=recipient.owner_id())
        assert all(s.id != share.id for s in inbox)
        assert await Share.get(share.id) is None

    async def test_deleting_the_recipient_removes_a_pending_incoming_offer(self):
        sender = await profile_service.create_profile(username="cleanup-in-sender")
        recipient = await profile_service.create_profile(username="cleanup-in-recipient")
        obj = await ready_object(owner=sender.owner_id())

        share = await share_service.offer_share(
            owner=sender.owner_id(), object_id=obj.id, to_profile_id=str(recipient.id)
        )

        await profile_service.delete_profile(recipient.id)

        outbox = await share_service.list_outbox(owner=sender.owner_id())
        assert all(s.id != share.id for s in outbox)
        assert await Share.get(share.id) is None

    async def test_an_accepted_share_survives_the_senders_deletion(self):
        sender = await profile_service.create_profile(username="cleanup-accepted-sender")
        recipient = await profile_service.create_profile(username="cleanup-accepted-recipient")
        obj = await ready_object(owner=sender.owner_id())

        share = await share_service.offer_share(
            owner=sender.owner_id(), object_id=obj.id, to_profile_id=str(recipient.id)
        )
        await share_service.accept_share(owner=recipient.owner_id(), share_id=share.id)
        await object_service.delete_object(obj.id, owner=sender.owner_id())
        await project_service.delete_project(obj.project_id, owner=sender.owner_id())

        await profile_service.delete_profile(sender.id)

        reloaded = await Share.get(share.id)
        assert reloaded is not None
        assert reloaded.state is ShareState.ACCEPTED

    async def test_an_accepted_share_survives_the_recipients_deletion(self):
        sender = await profile_service.create_profile(username="cleanup-accepted2-sender")
        recipient = await profile_service.create_profile(username="cleanup-accepted2-recipient")
        obj = await ready_object(owner=sender.owner_id())

        share = await share_service.offer_share(
            owner=sender.owner_id(), object_id=obj.id, to_profile_id=str(recipient.id)
        )
        copy = await share_service.accept_share(owner=recipient.owner_id(), share_id=share.id)
        await object_service.delete_object(copy.id, owner=recipient.owner_id())
        await project_service.delete_project(copy.project_id, owner=recipient.owner_id())

        await profile_service.delete_profile(recipient.id)

        reloaded = await Share.get(share.id)
        assert reloaded is not None
        assert reloaded.state is ShareState.ACCEPTED

    async def test_a_non_empty_profile_is_refused_before_any_share_is_touched(self):
        sender = await profile_service.create_profile(username="cleanup-guard-sender")
        recipient = await profile_service.create_profile(username="cleanup-guard-recipient")
        obj = await ready_object(owner=sender.owner_id())

        share = await share_service.offer_share(
            owner=sender.owner_id(), object_id=obj.id, to_profile_id=str(recipient.id)
        )

        with pytest.raises(ConflictError) as exc_info:
            await profile_service.delete_profile(sender.id)

        assert exc_info.value.code == "profile_not_empty"
        assert await Share.get(share.id) is not None
