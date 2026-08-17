"""bp:cancel must not accumulate ids for jobs that are already finished.

Every worker runs SMEMBERS bp:cancel once a second in _cancel_watch_loop, so a
stale entry is a cost paid forever by every worker. release.lua already clears
the flag on its drop path; these are the routes that bypass it.
"""

import pytest
from beanie import init_beanie
from pymongo import AsyncMongoClient

from app.config import settings
from app.models import ALL_MODELS, JobState
from app.models.job import Job, JobError
from tests.queue.test_lifecycle import LEASE_MS, NOW_MS, claim


@pytest.fixture(autouse=True)
async def _init_beanie_models(monkeypatch):
    """`_fail_blocked_job` writes through `Job` (a Beanie Document) via a raw
    Mongo update plus a `Job.find(...)` in `_release_dependents`, so the
    integration test below needs Beanie initialized against a real database --
    same pattern as `tests/storage/test_object_role.py`. Function-scoped
    (unlike that file) because this suite's tests actually perform I/O:
    pytest-asyncio hands each async test its own event loop by default, and a
    wider-scoped Motor client ends up bound to the wrong loop the moment a
    later test tries to use it.

    `_fail_blocked_job` also reaches for `app.db.client.get_db()` directly
    (a second, separately-initialized Mongo handle used for the conditional
    `update_one`), so that is patched to the same throwaway database rather
    than standing up the app's real connection singleton in a test.
    """
    client = AsyncMongoClient(settings.mongo_url, tz_aware=True)
    db = client["biopipe_test"]
    await init_beanie(database=db, document_models=ALL_MODELS)
    monkeypatch.setattr("app.db.client.get_db", lambda: db)
    yield
    await client.close()


class TestReaperClearsCancel:
    async def test_requeued_job_keeps_its_cancel_entry(self, redis, scripts, job_factory):
        """The flag must survive a requeue: the job runs again and still needs
        to observe that it was cancelled. This is the case the fix must NOT
        break, so it is asserted against the raw script."""
        await job_factory("job1")
        await claim(scripts)
        await redis.sadd("bp:cancel", "job1")

        await scripts["reap_expired"](
            keys=["bp:q:running", "bp:q:ready"],
            args=[NOW_MS + LEASE_MS + 1, 100],
        )

        assert await redis.sismember("bp:cancel", "job1") == 1
        assert await redis.zscore("bp:q:ready", "job1") is not None


class TestFailBlockedJobClearsCancel:
    async def test_clears_the_flag_for_a_dependency_failure(self, redis, monkeypatch):
        """A blocked job failed by its dependency is terminal and will never
        run, so its cancel flag has no reader left."""
        from app.queue import queue

        await redis.sadd("bp:cancel", "blocked1")
        monkeypatch.setattr(queue, "get_redis", lambda: redis)

        await queue._clear_cancel_flag("blocked1")

        assert await redis.sismember("bp:cancel", "blocked1") == 0

    async def test_is_safe_when_no_flag_was_set(self, redis, monkeypatch):
        from app.queue import queue

        monkeypatch.setattr(queue, "get_redis", lambda: redis)
        await queue._clear_cancel_flag("never-cancelled")
        assert await redis.sismember("bp:cancel", "never-cancelled") == 0

    async def test_survives_a_redis_outage(self, monkeypatch):
        """Cleanup is hygiene, not correctness -- it must never fail a job."""
        from app.queue import queue

        class Boom:
            async def srem(self, *a, **kw):
                raise ConnectionError("redis is down")

        monkeypatch.setattr(queue, "get_redis", lambda: Boom())
        await queue._clear_cancel_flag("job1")  # must not raise

    async def test_fail_blocked_job_itself_clears_the_flag(self, redis, monkeypatch):
        """The other tests in this class call `_clear_cancel_flag` directly,
        which proves the helper works but not that `_fail_blocked_job` still
        calls it -- a regression that deleted or reordered that one added
        line would pass every other test here. This drives the real function
        end to end against a real Job document."""
        from app.queue import queue

        culprit = Job(
            type="build_index",
            state=JobState.FAILED,
            error=JobError(code="boom", message="disk full", retryable=False),
        )
        await culprit.insert()

        blocked = Job(
            type="align", state=JobState.BLOCKED, depends_on=[culprit.id]
        )
        await blocked.insert()

        await redis.sadd("bp:cancel", str(blocked.id))
        monkeypatch.setattr(queue, "get_redis", lambda: redis)

        await queue._fail_blocked_job(blocked, [culprit])

        assert await redis.sismember("bp:cancel", str(blocked.id)) == 0
