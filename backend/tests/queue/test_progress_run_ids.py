"""run_ids on the published job.progress event.

A scalar `run_id` field on `Job` was rejected (models/run.py's `RunJob`
docstring): a deduplicated `build_index` job can serve two runs at once, so
`run_ids` is a list here too. These tests are at `_write_progress`'s own
level -- run_ids is resolved once by the caller (worker.py's `_start_job`)
and simply carried through the throttled writer, never re-queried.
"""

import pytest

from app.queue import queue
from app.queue.executor import JobExecutor

pytestmark = [pytest.mark.asyncio(loop_scope="module")]


class TestRunIdsOnThePublishedEvent:
    async def test_run_ids_are_included_when_present(self, monkeypatch):
        published = []

        async def _fake_publish(event_type, data, *, owner):
            published.append((event_type, data, owner))

        async def _fake_update_one(*args, **kwargs):
            return None

        monkeypatch.setattr(queue, "publish_event", _fake_publish)
        ex = JobExecutor("test-worker")

        class _FakeColl:
            async def update_one(self, *a, **k):
                return None

        class _FakeDb:
            jobs = _FakeColl()

        monkeypatch.setattr("app.queue.executor.get_db", lambda: _FakeDb())

        await ex._write_progress(
            "6a0000000000000000000001", 0, {"pct": 0.5}, owner="local", run_ids=["run_a", "run_b"]
        )

        assert len(published) == 1
        _, data, _ = published[0]
        assert set(data["run_ids"]) == {"run_a", "run_b"}

    async def test_run_ids_key_is_absent_when_empty(self, monkeypatch):
        """A job in no run must not carry a misleading empty list key -- the
        UI and #18's aggregation should be able to treat the key's absence
        as 'not part of any run' without special-casing []."""
        published = []

        async def _fake_publish(event_type, data, *, owner):
            published.append((event_type, data, owner))

        class _FakeColl:
            async def update_one(self, *a, **k):
                return None

        class _FakeDb:
            jobs = _FakeColl()

        monkeypatch.setattr(queue, "publish_event", _fake_publish)
        monkeypatch.setattr("app.queue.executor.get_db", lambda: _FakeDb())

        ex = JobExecutor("test-worker")
        await ex._write_progress("6a0000000000000000000001", 0, {"pct": 0.5}, owner="local", run_ids=[])

        _, data, _ = published[0]
        assert "run_ids" not in data
