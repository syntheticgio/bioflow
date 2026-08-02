"""Maintenance jobs are enqueued under the system owner, not a real profile's.

`tick` and `run_now` fire the installation's own housekeeping -- GC, file
verification, the reapers -- which belongs to the machine rather than to
anyone's library. They used to say so with a hardcoded `owner="local"`, and
that was wrong for a reason the string hides: `"local"` is not a neutral
sentinel, it is the owner value of whichever profile adopted the pre-profiles
library (`Profile.owner_id`). So every maintenance job landed in one real
person's queue and, because job events route by `Job.owner`, only that person's
event stream.

The assertion here is deliberately `== keys.SYSTEM_OWNER` rather than
`!= "local"`. `owner` is inherited from `TimestampedDocument` and *defaults* to
`"local"`, so a test that only rejected `"local"` would fail for a job whose
owner was never threaded through at all -- but one that merely rejected it
would pass for any typo'd non-empty string. Naming the expected value is what
distinguishes "the system owner was passed" from "something was passed".

The route half of this -- every profile seeing these jobs, and nobody's private
jobs leaking in alongside them -- is in
`tests/api/test_route_owner_scoping.py::TestJobsRouter`. Both halves are needed:
this module proves the jobs are stamped `system`, that one proves `system` is
actually visible.
"""

import pytest
from beanie import PydanticObjectId

from app.models import JobClass, Schedule
from app.queue import keys, scheduler
from app.queue import queue as queue_mod

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest.fixture
def captured_enqueues(monkeypatch):
    """Capture `enqueue` kwargs instead of queueing.

    Both call sites resolve `queue` through a function-local import, which
    re-reads the attribute off the module every call -- so patching
    `app.queue.queue.enqueue` is what they see. Returning a stub job rather
    than `None` matters for `tick`: a `None` return is its "deduplicated away"
    branch, which skips the schedule bookkeeping the real path performs.
    """
    calls: list[dict] = []

    class _StubJob:
        # A real ObjectId, not a marker string: `tick` writes this into
        # `Schedule.last_job_id`, which is typed `PydanticObjectId`, so a
        # placeholder fails validation on the way out rather than in the
        # assertion, and the failure reads as unrelated.
        id = PydanticObjectId()

    async def _capture(job_type, **kwargs):
        calls.append({"job_type": job_type, **kwargs})
        return _StubJob()

    monkeypatch.setattr(queue_mod, "enqueue", _capture)
    return calls


async def _schedule(name: str, *, interval_seconds: int = 60) -> Schedule:
    return await Schedule(
        id=name,
        job_type=name,
        interval_seconds=interval_seconds,
        job_class=JobClass.MAINTENANCE,
        payload={},
        enabled=True,
        catchup=False,
    ).insert()


class TestRunNow:
    async def test_a_forced_run_is_owned_by_the_system(self, captured_enqueues):
        """Pressing "Run now" does not make the sweep belong to whoever pressed.

        `run_now` takes no owner and never had one to inherit -- the route is
        one of the five deliberately-unscoped ones on /schedules. What changed
        is which owner it hardcodes.
        """
        await _schedule("gc_blobs_run_now_test")

        await scheduler.run_now("gc_blobs_run_now_test")

        assert len(captured_enqueues) == 1
        assert captured_enqueues[0]["owner"] == keys.SYSTEM_OWNER

    async def test_a_forced_run_still_jumps_the_maintenance_queue(
        self, captured_enqueues
    ):
        """The property the owner change must not have disturbed: a human asked
        for this, so it runs at user priority rather than behind maintenance."""
        await _schedule("verify_files_priority_test")

        await scheduler.run_now("verify_files_priority_test")

        assert captured_enqueues[0]["job_class"] is JobClass.USER_INTERACTIVE

    async def test_an_unknown_schedule_enqueues_nothing(self, captured_enqueues):
        result = await scheduler.run_now("no_such_schedule")

        assert result is None
        assert captured_enqueues == []


class TestTick:
    async def test_a_due_schedule_fires_under_the_system_owner(
        self, captured_enqueues, monkeypatch
    ):
        """The periodic path, which is where the great majority of these jobs
        come from -- `run_now` is the rare manual case.

        The Redis claim is stubbed to "won": it is an atomic
        compare-and-advance in a Lua script, and this process has no Redis. It
        decides *whether* a schedule fires, not what owner the resulting job
        carries, so stubbing it leaves the assertion intact.
        """
        await _schedule("gc_blobs_tick_test")

        def _always_win(_name):
            async def _run(**_kwargs):
                return 1

            return _run

        monkeypatch.setattr(scheduler, "get_script", _always_win)

        await scheduler.tick()

        owners = {c["owner"] for c in captured_enqueues}
        assert owners == {keys.SYSTEM_OWNER}
