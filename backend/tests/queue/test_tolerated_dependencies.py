"""Branch-scoped failure: a dependency whose failure must not cascade.

`classify_dependencies` already knows how to spare a tolerated dependency
(tests/queue/test_dependencies.py), but knowing is not doing: both routes that
actually fail a dependent re-read `depends_on` from the database and never pass
the tolerated set, so the pure function's parameter had no reachable caller.
These drive the two routes end to end against real documents.

The two routes are deliberately both covered. `_release_dependents` is the one
the design names, but `enqueue` re-reads its dependencies after inserting
and fails the job itself if any had already finished badly -- so fixing only
the first leaves a race where a tolerated dependency that failed *before* its
dependent was enqueued still kills it. That interleaving is rare and entirely
silent, which is exactly the kind that survives a green suite.
"""

import pytest
from beanie import init_beanie
from pymongo import AsyncMongoClient

from app.config import settings
from app.models import ALL_MODELS, JobState
from app.models.job import Job, JobError


@pytest.fixture(autouse=True)
async def _init_beanie_models(monkeypatch):
    """Same shape as tests/queue/test_cancel_cleanup.py: the code under test
    writes through both Beanie and a separately-initialized `get_db()` handle,
    so both are pointed at one throwaway database. Function-scoped because
    these tests perform real I/O and pytest-asyncio gives each async test its
    own event loop."""
    client = AsyncMongoClient(settings.mongo_url, tz_aware=True)
    db = client["biopipe_test"]
    await init_beanie(database=db, document_models=ALL_MODELS)
    monkeypatch.setattr("app.db.client.get_db", lambda: db)
    yield
    await client.close()


@pytest.fixture(autouse=True)
def _quiet_redis(redis, monkeypatch):
    """`_fail_blocked_job` and the release path both touch Redis for cancel-flag
    cleanup and dispatch. Neither is what these tests assert on, but both must
    not blow up."""
    from app.queue import queue

    monkeypatch.setattr(queue, "get_redis", lambda: redis)


async def _failed(job_type: str = "qc") -> Job:
    job = Job(
        type=job_type,
        state=JobState.FAILED,
        error=JobError(code="boom", message="disk full", retryable=False),
    )
    await job.insert()
    return job


async def _succeeded(job_type: str = "build_index") -> Job:
    job = Job(type=job_type, state=JobState.SUCCEEDED)
    await job.insert()
    return job


class TestReleaseDependentsHonoursTolerance:
    """The route the design names: a dependency reaches a terminal state and
    the jobs waiting on it are released or failed."""

    async def test_a_tolerated_failure_releases_the_dependent(self):
        """The whole point of `continue_on_failure`: a QC node failing means we
        lack a report, not that the assembly behind it is unusable."""
        from app.queue import queue

        culprit = await _failed()
        dependent = Job(
            type="align",
            state=JobState.BLOCKED,
            depends_on=[culprit.id],
            tolerate_failure_of=[culprit.id],
        )
        await dependent.insert()

        await queue._release_dependents(str(culprit.id), succeeded=False)

        fresh = await Job.get(dependent.id)
        assert fresh.state is not JobState.FAILED
        assert fresh.state is not JobState.BLOCKED

    async def test_an_untolerated_failure_still_fails_the_dependent(self):
        """Today's behaviour, and the one that must not regress: every existing
        caller sets no tolerance at all."""
        from app.queue import queue

        culprit = await _failed("build_index")
        dependent = Job(type="align", state=JobState.BLOCKED, depends_on=[culprit.id])
        await dependent.insert()

        await queue._release_dependents(str(culprit.id), succeeded=False)

        fresh = await Job.get(dependent.id)
        assert fresh.state is JobState.FAILED

    async def test_a_mixed_dependent_fails_naming_the_untolerated_one(self):
        """Tolerance is per-edge. A node depending on both an optional QC step
        and a mandatory alignment is doomed by the second one only -- and the
        error must quote the dependency that actually doomed it, or the user
        goes hunting for a cause that was survivable."""
        from app.queue import queue

        tolerated = await _failed("qc")
        fatal = await _failed("build_index")
        dependent = Job(
            type="align",
            state=JobState.BLOCKED,
            depends_on=[tolerated.id, fatal.id],
            tolerate_failure_of=[tolerated.id],
        )
        await dependent.insert()

        await queue._release_dependents(str(fatal.id), succeeded=False)

        fresh = await Job.get(dependent.id)
        assert fresh.state is JobState.FAILED
        assert str(fatal.id) in fresh.error.message
        assert str(tolerated.id) not in fresh.error.message

    async def test_a_tolerated_dependency_still_blocks_while_a_sibling_runs(self):
        """Tolerating failure is not the same as not waiting. The dependent
        must stay put until every dependency is out of flight."""
        from app.queue import queue

        tolerated = await _failed("qc")
        running = Job(type="build_index", state=JobState.RUNNING)
        await running.insert()
        dependent = Job(
            type="align",
            state=JobState.BLOCKED,
            depends_on=[tolerated.id, running.id],
            tolerate_failure_of=[tolerated.id],
        )
        await dependent.insert()

        await queue._release_dependents(str(tolerated.id), succeeded=False)

        fresh = await Job.get(dependent.id)
        assert fresh.state is JobState.BLOCKED


class TestEnqueueHonoursTolerance:
    """The route the design does not name. A dependency that failed *before*
    the dependent was enqueued is caught by `enqueue`'s own post-insert
    re-read, on a code path `_release_dependents` never reaches."""

    async def test_a_tolerated_already_failed_dependency_does_not_fail_the_job(self):
        from app.queue.queue import enqueue

        culprit = await _failed()

        job = await enqueue(
            "align",
            owner="tester",
            depends_on=[culprit.id],
            tolerate_failure_of=[culprit.id],
        )

        fresh = await Job.get(job.id)
        assert fresh.state is not JobState.FAILED

    async def test_an_untolerated_already_failed_dependency_still_fails_the_job(self):
        from app.queue.queue import enqueue

        culprit = await _failed("build_index")

        job = await enqueue("align", owner="tester", depends_on=[culprit.id])

        fresh = await Job.get(job.id)
        assert fresh.state is JobState.FAILED

    async def test_a_tolerated_failure_alongside_a_success_dispatches(self):
        """With nothing left in flight and the only failure tolerated, the job
        is runnable immediately -- it must not be parked in BLOCKED waiting for
        a release that will never come."""
        from app.queue.queue import enqueue

        tolerated = await _failed("qc")
        done = await _succeeded()

        job = await enqueue(
            "align",
            owner="tester",
            depends_on=[tolerated.id, done.id],
            tolerate_failure_of=[tolerated.id],
        )

        fresh = await Job.get(job.id)
        assert fresh.state is JobState.QUEUED


class TestEnqueueDoesNotDispatchAHeldJob:
    """`enqueue`'s docstring: a job with unsatisfied dependencies "is never
    pushed to Redis". `_handle_dependencies` runs for its side effects and
    returns None in every branch, so the state it writes is only durable if
    `enqueue` then stops -- and `_push_to_redis` ends with an unconditional
    `job.set({Job.state: ...})` that overwrites it.

    The failure is invisible from the state alone: the job is also pushed onto
    the ready set, so a worker claims it. An alignment runs against an index
    that failed to build, or that is still building.
    """

    async def test_a_failed_dependency_leaves_the_job_failed_not_queued(self):
        from app.queue.queue import enqueue

        culprit = await _failed("build_index")

        job = await enqueue("align", owner="tester", depends_on=[culprit.id])

        fresh = await Job.get(job.id)
        assert fresh.state is JobState.FAILED

    async def test_a_failed_dependency_keeps_the_job_off_the_ready_set(self, redis):
        """The state is the symptom; this is the damage. A doomed job on the
        ready set is claimable."""
        from app.queue import keys
        from app.queue.queue import enqueue

        culprit = await _failed("build_index")

        job = await enqueue("align", owner="tester", depends_on=[culprit.id])

        assert await redis.zscore(keys.ready_key(None), str(job.id)) is None

    async def test_an_unfinished_dependency_leaves_the_job_blocked(self):
        """Nothing failed here -- the dependency is simply still running, which
        is the ordinary case every wired-up pipeline hits."""
        from app.queue.queue import enqueue

        running = Job(type="build_index", state=JobState.RUNNING)
        await running.insert()

        job = await enqueue("align", owner="tester", depends_on=[running.id])

        fresh = await Job.get(job.id)
        assert fresh.state is JobState.BLOCKED

    async def test_an_unfinished_dependency_keeps_the_job_off_the_ready_set(
        self, redis
    ):
        from app.queue import keys
        from app.queue.queue import enqueue

        running = Job(type="build_index", state=JobState.RUNNING)
        await running.insert()

        job = await enqueue("align", owner="tester", depends_on=[running.id])

        assert await redis.zscore(keys.ready_key(None), str(job.id)) is None


class TestDefaultsAreUnchanged:
    async def test_a_job_defaults_to_tolerating_nothing(self):
        """Every existing caller relies on this. A default that tolerated
        anything would silently convert real dependency failures into jobs that
        run against missing inputs."""
        job = Job(type="align", state=JobState.PENDING)
        assert job.tolerate_failure_of == []
