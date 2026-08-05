"""eta_seconds on the published job.progress event -- derived, never stored.

`_write_progress` computes it from `started_at` (cached on JobContext at
claim time) and whatever `pct`/`eta_model_ms` are available for this specific
tick, and puts it only on the outgoing event dict -- never into the `$set`
that lands on `progress.*` in Mongo. A stored ETA would be wrong by exactly
the time since it was written; deriving it at emit time is the whole point.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.queue.executor import JobExecutor

pytestmark = [pytest.mark.asyncio(loop_scope="module")]


class _FakeColl:
    def __init__(self):
        self.sets: list[dict] = []

    async def update_one(self, filter_, update):
        self.sets.append(update["$set"])


class _FakeDb:
    def __init__(self, coll):
        self.jobs = coll


class TestEtaOnThePublishedEvent:
    async def test_eta_is_derived_from_elapsed_and_pct(self, monkeypatch):
        published = []

        async def _fake_publish(event_type, data, *, owner):
            published.append(data)

        coll = _FakeColl()
        monkeypatch.setattr("app.queue.executor.get_db", lambda: _FakeDb(coll))
        monkeypatch.setattr("app.queue.queue.publish_event", _fake_publish)

        started_at = datetime.now(UTC) - timedelta(seconds=100)
        ex = JobExecutor("test-worker")
        await ex._write_progress(
            "6a0000000000000000000001",
            0,
            {"pct": 0.5},
            owner="local",
            started_at=started_at,
            eta_model_ms=None,
        )

        assert len(published) == 1
        assert published[0]["eta_seconds"] == pytest.approx(100.0, rel=0.05)

    async def test_eta_is_never_written_to_the_progress_document(self, monkeypatch):
        """The regression a careless implementation would introduce: adding
        eta_seconds to the $set dict would persist a number that goes stale
        the instant it is written."""
        published = []

        async def _fake_publish(event_type, data, *, owner):
            published.append(data)

        coll = _FakeColl()
        monkeypatch.setattr("app.queue.executor.get_db", lambda: _FakeDb(coll))
        monkeypatch.setattr("app.queue.queue.publish_event", _fake_publish)

        started_at = datetime.now(UTC) - timedelta(seconds=50)
        ex = JobExecutor("test-worker")
        await ex._write_progress(
            "6a0000000000000000000001",
            0,
            {"pct": 0.5},
            owner="local",
            started_at=started_at,
            eta_model_ms=None,
        )

        assert len(coll.sets) == 1
        assert "progress.eta_seconds" not in coll.sets[0]
        assert not any(k.startswith("progress.eta") for k in coll.sets[0])

    async def test_no_started_at_means_no_eta_key(self, monkeypatch):
        """The fallback JobContext construction (no worker-supplied ctx) may
        not have a started_at; the event must simply omit the key rather than
        crash or publish a nonsensical eta."""
        published = []

        async def _fake_publish(event_type, data, *, owner):
            published.append(data)

        coll = _FakeColl()
        monkeypatch.setattr("app.queue.executor.get_db", lambda: _FakeDb(coll))
        monkeypatch.setattr("app.queue.queue.publish_event", _fake_publish)

        ex = JobExecutor("test-worker")
        await ex._write_progress(
            "6a0000000000000000000001", 0, {"pct": 0.5}, owner="local"
        )

        assert "eta_seconds" not in published[0]

    async def test_no_pct_and_no_model_omits_eta_even_with_started_at(self, monkeypatch):
        published = []

        async def _fake_publish(event_type, data, *, owner):
            published.append(data)

        coll = _FakeColl()
        monkeypatch.setattr("app.queue.executor.get_db", lambda: _FakeDb(coll))
        monkeypatch.setattr("app.queue.queue.publish_event", _fake_publish)

        started_at = datetime.now(UTC) - timedelta(seconds=10)
        ex = JobExecutor("test-worker")
        await ex._write_progress(
            "6a0000000000000000000001",
            0,
            {"rss_bytes": 1024},
            owner="local",
            started_at=started_at,
            eta_model_ms=None,
        )

        assert "eta_seconds" not in published[0]
