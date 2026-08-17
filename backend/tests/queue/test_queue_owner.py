"""`enqueue` stamps an owner, and the dedup key is scoped to it.

The trap here is quiet enough to be worth naming. `enqueue` deduplicates
through a unique partial index over `dedup_key` alone, and on a collision it
returns `None` -- no exception, no log the user ever sees, just a job that was
never created. Two of the keys in this codebase are built purely from content
(`build_index:{digest}:{aligner}` and `index_bam:{sha256}`), and blobs are
global and shared by design. So before this change, the second profile to align
against a reference genome another profile was already indexing got `None` back
and its index simply never happened.

These tests use non-"local" owners on purpose: "local" is the default every
`Job` inherits from `TimestampedDocument`, so an assertion against it passes
whether or not anything was actually threaded through.
"""

import uuid

import pytest
from app.models import Job, JobState
from app.queue import queue

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    """Keep the Mongo insert -- the actual dedup guard -- and stub the rest.

    `enqueue` writes Mongo first and then pushes to Redis, and it is only the
    first half that decides whether a job exists. This process has no Redis, so
    the push and its event publish are stubbed; stubbing the insert instead
    would remove the very thing under test.
    """

    async def _skip(*args, **kwargs):
        return None

    monkeypatch.setattr(queue, "_push_to_redis", _skip)
    monkeypatch.setattr(queue, "publish_event", _skip)


def _key() -> str:
    """A dedup key unique to one test.

    The unique index only covers non-terminal jobs, but nothing here reaches a
    terminal state, so a key shared between tests would have one test's leftover
    job deduplicate away another's.
    """
    return f"test_owner:{uuid.uuid4().hex}"


class TestEnqueueStampsOwner:
    async def test_the_job_carries_the_owner_it_was_given(self):
        job = await queue.enqueue("noop", owner="queue-owner-a", dedup_key=_key())

        assert job is not None
        assert job.owner == "queue-owner-a"

    async def test_the_owner_survives_the_round_trip_to_mongo(self):
        """Read back rather than trusting the in-memory document: the owner has
        to be on the stored row for `delete_project_tree`'s cascade filter and
        every future owner-scoped query to see it."""
        job = await queue.enqueue("noop", owner="queue-owner-b", dedup_key=_key())

        assert job is not None
        stored = await Job.get(job.id)
        assert stored is not None
        assert stored.owner == "queue-owner-b"


class TestDedupIsScopedToOwner:
    async def test_two_owners_sharing_a_dedup_key_both_get_a_job(self):
        """The trap, stated directly.

        `build_index:{digest}:{aligner}` is the real key this stands in for: it
        is derived from blob content alone, and blobs are shared across
        profiles. Without the owner fold the second call returns `None` and
        that profile's index is never built -- silently, since `enqueue`
        reports deduplication as a `None` return rather than an error.
        """
        shared = _key()

        first = await queue.enqueue("build_index", owner="queue-owner-c", dedup_key=shared)
        second = await queue.enqueue("build_index", owner="queue-owner-d", dedup_key=shared)

        assert first is not None
        assert second is not None, "the second owner's job was deduplicated away"
        assert first.id != second.id
        assert {first.owner, second.owner} == {"queue-owner-c", "queue-owner-d"}

    async def test_the_stored_key_carries_the_owner(self):
        """The mechanism, so a future reader can see *why* the two coexist
        rather than concluding the unique index stopped working."""
        key = _key()
        job = await queue.enqueue("build_index", owner="queue-owner-e", dedup_key=key)

        assert job is not None
        assert job.dedup_key == f"queue-owner-e:{key}"

    async def test_no_dedup_key_still_means_no_deduplication(self):
        """A `None` key must stay `None`, not become the bare owner string --
        every job that opts out of deduplication would otherwise collide with
        every other job from the same profile."""
        first = await queue.enqueue("noop", owner="queue-owner-f")
        second = await queue.enqueue("noop", owner="queue-owner-f")

        assert first is not None
        assert second is not None
        assert first.dedup_key is None
        assert second.dedup_key is None


class TestDedupStillProtectsOneOwner:
    async def test_the_same_owner_repeating_a_key_is_deduplicated(self):
        """The protection that must survive the fix: a double-submit from one
        profile still collapses into a single job."""
        key = _key()

        first = await queue.enqueue("run_qc", owner="queue-owner-g", dedup_key=key)
        second = await queue.enqueue("run_qc", owner="queue-owner-g", dedup_key=key)

        assert first is not None
        assert second is None

    async def test_a_terminal_job_frees_its_key_for_the_same_owner(self):
        """The partial index only covers non-terminal states, so a finished job
        must not block a re-run. Asserted because folding the owner in changes
        the stored *value*, and a value change is exactly the kind of thing
        that can accidentally fall outside a partial index's filter."""
        key = _key()

        first = await queue.enqueue("run_qc", owner="queue-owner-h", dedup_key=key)
        assert first is not None
        await first.set({Job.state: JobState.SUCCEEDED})

        again = await queue.enqueue("run_qc", owner="queue-owner-h", dedup_key=key)
        assert again is not None
        assert again.id != first.id
