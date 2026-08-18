"""The five /schedules routes: list, overdue, get, patch, and run-now.

These are the one router in `api/v1` that is deliberately *not* owner-scoped --
`schedules.py`'s module docstring spells out why -- so unlike its neighbours
there are no `two_profiles` headers here. What is left to assert is the part
the owner-scoping sweep cannot reach: that a missing name 404s rather than
500s, that a PATCH persists, and that shortening an interval clears the Redis
tick marker.

`reset_next_run` and `run_now` both touch Redis, which this process does not
have; they are stubbed the same way `test_exports_api.py` stubs the queue push.
"""

import pytest
import pytest_asyncio

from app.models import JobClass, Schedule
from app.queue import scheduler

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest_asyncio.fixture(loop_scope="module")
async def a_schedule():
    """One schedule, removed afterwards.

    Created and torn down per test rather than seeded once for the module:
    `test_patch_persists_the_change` mutates it, and the GET assertions below
    would then depend on test order.
    """
    schedule = Schedule(
        id="gc_blobs",
        job_type="gc_blobs",
        interval_seconds=3600,
        job_class=JobClass.MAINTENANCE,
        payload={"dry_run": True},
    )
    await schedule.insert()
    yield schedule
    await schedule.delete()


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(scheduler, "reset_next_run", _noop)


class TestList:
    async def test_lists_the_schedules(self, client, a_schedule):
        resp = await client.get("/api/v1/schedules")

        assert resp.status_code == 200, resp.text
        names = [s["name"] for s in resp.json()]
        assert "gc_blobs" in names

    async def test_reports_the_fields_the_ui_renders(self, client, a_schedule):
        resp = await client.get("/api/v1/schedules")

        entry = next(s for s in resp.json() if s["name"] == "gc_blobs")
        assert entry["job_type"] == "gc_blobs"
        assert entry["interval_seconds"] == 3600
        assert entry["job_class"] == "maintenance"
        assert entry["payload"] == {"dry_run": True}
        assert entry["enabled"] is True
        assert entry["last_run_at"] is None
        assert entry["last_job_id"] is None


class TestGet:
    async def test_returns_one_schedule_by_name(self, client, a_schedule):
        resp = await client.get("/api/v1/schedules/gc_blobs")

        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "gc_blobs"

    async def test_404s_for_an_unknown_name(self, client):
        resp = await client.get("/api/v1/schedules/no_such_schedule")

        assert resp.status_code == 404


class TestPatch:
    async def test_persists_the_change(self, client, a_schedule):
        resp = await client.patch(
            "/api/v1/schedules/gc_blobs", json={"enabled": False}
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["enabled"] is False
        assert (await Schedule.get("gc_blobs")).enabled is False

    async def test_leaves_unmentioned_fields_alone(self, client, a_schedule):
        """`exclude_unset` is what makes a one-field PATCH safe; without it a
        toggle of `enabled` would reset the interval to the model default."""
        resp = await client.patch(
            "/api/v1/schedules/gc_blobs", json={"enabled": False}
        )

        assert resp.json()["interval_seconds"] == 3600
        assert resp.json()["payload"] == {"dry_run": True}

    async def test_clears_the_tick_marker_when_the_interval_changes(
        self, client, a_schedule, monkeypatch
    ):
        """The next fire time lives in Redis, so a shortened interval does not
        take effect until the old one elapses unless the marker is cleared."""
        cleared = []

        async def _record(name):
            cleared.append(name)

        monkeypatch.setattr(scheduler, "reset_next_run", _record)

        resp = await client.patch(
            "/api/v1/schedules/gc_blobs", json={"interval_seconds": 60}
        )

        assert resp.status_code == 200, resp.text
        assert cleared == ["gc_blobs"]

    async def test_does_not_clear_the_marker_when_the_interval_is_unchanged(
        self, client, a_schedule, monkeypatch
    ):
        cleared = []

        async def _record(name):
            cleared.append(name)

        monkeypatch.setattr(scheduler, "reset_next_run", _record)

        await client.patch("/api/v1/schedules/gc_blobs", json={"interval_seconds": 3600})

        assert cleared == []

    @pytest.mark.parametrize("interval", [4, 86401])
    async def test_rejects_an_out_of_range_interval(self, client, a_schedule, interval):
        resp = await client.patch(
            "/api/v1/schedules/gc_blobs", json={"interval_seconds": interval}
        )

        assert resp.status_code == 422

    async def test_404s_for_an_unknown_name(self, client):
        resp = await client.patch(
            "/api/v1/schedules/no_such_schedule", json={"enabled": False}
        )

        assert resp.status_code == 404


class TestOverdue:
    async def test_reports_nothing_for_a_schedule_that_has_never_run(
        self, client, a_schedule
    ):
        """`last_run_at is None` is a fresh install, not a stalled sweep."""
        resp = await client.get("/api/v1/schedules/overdue")

        assert resp.status_code == 200, resp.text
        assert [o["name"] for o in resp.json()["overdue"]] == []

    async def test_reports_a_schedule_that_has_not_run_in_many_intervals(
        self, client, a_schedule
    ):
        from datetime import UTC, datetime, timedelta

        a_schedule.last_run_at = datetime.now(UTC) - timedelta(seconds=3600 * 20)
        await a_schedule.save()

        resp = await client.get("/api/v1/schedules/overdue")

        entry = next(o for o in resp.json()["overdue"] if o["name"] == "gc_blobs")
        assert entry["interval_seconds"] == 3600
        assert entry["seconds_overdue"] > 0

    async def test_overdue_is_not_read_as_a_schedule_name(self, client):
        """`/overdue` is declared before `/{name}`; were it declared after, this
        would 404 as a schedule called "overdue" rather than returning a list."""
        resp = await client.get("/api/v1/schedules/overdue")

        assert resp.status_code == 200
        assert "overdue" in resp.json()


class TestRunNow:
    async def test_returns_the_job_it_enqueued(self, client, a_schedule, monkeypatch):
        async def _run_now(name):
            return "job-123"

        monkeypatch.setattr(scheduler, "run_now", _run_now)

        resp = await client.post("/api/v1/schedules/gc_blobs/run-now")

        assert resp.status_code == 202, resp.text
        assert resp.json() == {"name": "gc_blobs", "job_id": "job-123"}

    async def test_404s_for_an_unknown_name(self, client, monkeypatch):
        async def _run_now(name):
            return None

        monkeypatch.setattr(scheduler, "run_now", _run_now)

        resp = await client.post("/api/v1/schedules/no_such_schedule/run-now")

        assert resp.status_code == 404

    async def test_409s_when_an_identical_run_is_already_queued(
        self, client, a_schedule, monkeypatch
    ):
        """`run_now` returns None both for a missing schedule and for a dedup
        hit; the route tells them apart by re-reading the document, and getting
        that backwards would report a queued job as a missing schedule."""
        async def _run_now(name):
            return None

        monkeypatch.setattr(scheduler, "run_now", _run_now)

        resp = await client.post("/api/v1/schedules/gc_blobs/run-now")

        assert resp.status_code == 422, resp.text
