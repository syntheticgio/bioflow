"""An applier that refuses to write its result must be able to fail the job.

`_apply_result` catches every exception and logs `result_apply_failed`, so a
job whose applier gave up still reports *succeeded*. That is deliberate for
the case it was written for -- an incidental write-back failure after the tool
already ran should not throw away expensive work by re-running it -- but it
also swallows the opposite case, where the applier has *decided* the job
produced nothing usable.

`_apply_align_reads_chunked` is that case (#595): it refuses to merge a
partial set of bucket BAMs, and the user sees a green alignment job with no
alignment object and only a line in the worker log to say why.

So `PermanentError` is carved out as a narrow, opt-in channel meaning "the
applier decided this job's output is not usable". It reaches the executor's
existing `except PermanentError` branch, which fails the job without burning
retries. Every other exception stays swallowed exactly as before -- these
tests pin both halves, because a fix that made *all* apply failures fatal
would re-run hours of alignment over a transient Mongo blip.
"""

from datetime import UTC, datetime

import pytest

from app.errors import PermanentError
from app.models import Job, JobClass, JobResources, JobState
from app.models.job import JobLease
from app.queue import queue, results
from app.queue.executor import JobExecutor
from app.queue.registry import HandlerMode, HandlerSpec

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    """`queue.complete` releases the lease and publishes over Redis, neither of
    which this process has. The Mongo write that carries the job's terminal
    state stays real -- that is what these tests read."""

    async def _skip(*args, **kwargs):
        return True

    monkeypatch.setattr(queue, "release", _skip)
    monkeypatch.setattr(queue, "publish_event", _skip)


def _spec(fn) -> HandlerSpec:
    return HandlerSpec(
        name="test_handler",
        fn=fn,
        mode=HandlerMode.ASYNC,
        default_class=JobClass.USER_BACKGROUND,
        default_resources=JobResources(),
        max_attempts=5,
    )


async def _make_job(job_type: str) -> Job:
    """A running job holding the lease at epoch 0.

    The lease is not incidental: every terminal write in `queue.complete` is
    conditional on `lease.epoch` matching, so a job without one has all of its
    completions rejected as stale and stays RUNNING no matter what the
    executor decides.
    """
    now = datetime.now(UTC)
    job = Job(
        type=job_type,
        state=JobState.RUNNING,
        payload={},
        owner="local",
        lease=JobLease(worker_id="test-worker", expires_at=now, heartbeat_at=now, epoch=0),
    )
    job.timing.enqueued_at = now
    job.timing.started_at = now
    await job.insert()
    return job


async def _run_with_applier(job_type: str, applier, monkeypatch) -> Job:
    """Run one job whose handler succeeds and whose applier is `applier`."""
    monkeypatch.setitem(results._APPLIERS, job_type, applier)
    job = await _make_job(job_type)

    async def ok(ctx):
        return {"produced": "something"}

    await JobExecutor("test-worker").run(job, _spec(ok), epoch=0)
    return await Job.get(job.id)


class TestAPermanentErrorFromAnApplierFailsTheJob:
    async def test_the_job_reports_failed_not_succeeded(self, monkeypatch):
        """The whole point of #595: the user must not see a green job that
        produced nothing."""

        async def refuse(result, *, owner):
            raise PermanentError("resolved 3 of 4 buckets")

        job = await _run_with_applier("applier_refuses", refuse, monkeypatch)

        assert job.state == JobState.FAILED

    async def test_the_refusal_reason_reaches_the_job_error(self, monkeypatch):
        """A failed job with an empty error is barely better than a green one
        -- the message the applier raised is the only explanation the user
        gets, so it has to survive to the document the UI reads."""

        async def refuse(result, *, owner):
            raise PermanentError("resolved 3 of 4 bucket alignments")

        job = await _run_with_applier("applier_refusal_reason", refuse, monkeypatch)

        assert job.error is not None
        assert "3 of 4" in job.error.message

    async def test_the_refusal_does_not_burn_retries(self, monkeypatch):
        """`PermanentError` means re-running cannot help. The executor's
        existing permanent branch fails the job outright rather than sending
        it back for four more attempts at the same refusal."""

        async def refuse(result, *, owner):
            raise PermanentError("nope")

        job = await _run_with_applier("applier_no_retry", refuse, monkeypatch)

        assert job.state == JobState.FAILED
        assert job.attempts == 0
        assert job.error is not None
        assert job.error.retryable is False


class TestEveryOtherApplyFailureIsStillSwallowed:
    """The deliberate behaviour `_apply_result` was written for. A fix that
    made all apply failures fatal would re-run hours of alignment because a
    Mongo write blipped, which is the exact trade the catch-all exists to
    avoid."""

    async def test_an_unexpected_exception_leaves_the_job_succeeded(
        self, monkeypatch
    ):
        async def boom(result, *, owner):
            raise RuntimeError("connection reset by peer")

        job = await _run_with_applier("applier_incidental_failure", boom, monkeypatch)

        assert job.state == JobState.SUCCEEDED
        assert job.error is None

    async def test_the_handler_result_is_still_recorded(self, monkeypatch):
        """The tool ran and its output is on disk; the result has to stay on
        the job so the work is not invisible as well as unapplied."""

        async def boom(result, *, owner):
            raise ValueError("bad document")

        job = await _run_with_applier("applier_result_survives", boom, monkeypatch)

        assert job.result == {"produced": "something"}


class TestASuccessfulApplierIsUnaffected:
    async def test_the_job_succeeds_and_the_applier_ran(self, monkeypatch):
        seen: list[dict] = []

        async def record(result, *, owner):
            seen.append(result)

        job = await _run_with_applier("applier_succeeds", record, monkeypatch)

        assert job.state == JobState.SUCCEEDED
        assert seen == [{"produced": "something"}]
