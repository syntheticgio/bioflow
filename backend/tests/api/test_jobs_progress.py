"""GET /jobs and GET /jobs/{id} carry the widened progress model.

JobOut's `progress` field is a plain dict from `model_dump(mode="json")`, so
the new JobProgress fields flow through with no extra wiring -- but that is a
claim worth a test rather than an assumption, since the point of the task is
that the wire format actually changed. `last_attempt_progress` needs its own
field on JobOut and is checked explicitly.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.models import AttemptProgress, Job, JobClass, JobProgress, JobResources, JobState

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


async def _make_job(owner: str, **kwargs) -> Job:
    kwargs.setdefault("state", JobState.RUNNING)
    job = Job(
        type="jobs_progress_probe",
        payload={},
        owner=owner,
        job_class=JobClass.USER_BACKGROUND,
        resources=JobResources(),
        **kwargs,
    )
    await job.insert()
    return job


class TestProgressFieldsOnJobDetail:
    async def test_null_pct_and_new_fields_round_trip(self, client, two_profiles):
        owner = two_profiles["a"].owner_id()
        job = await _make_job(
            owner,
            progress=JobProgress(
                pct=None,
                phase="assembling",
                units_done=3,
                units_total=7,
                unit_label="chunks",
                rss_bytes=1024,
                cpu_percent=12.5,
                peak_rss_bytes=2048,
                peak_cpu_percent=40.0,
            ),
        )

        r = await client.get(
            f"/api/v1/jobs/{job.id}", headers=two_profiles["a_headers"]
        )

        assert r.status_code == 200
        progress = r.json()["progress"]
        assert progress["pct"] is None
        assert progress["phase"] == "assembling"
        assert progress["units_done"] == 3
        assert progress["units_total"] == 7
        assert progress["unit_label"] == "chunks"
        assert progress["rss_bytes"] == 1024
        assert progress["cpu_percent"] == 12.5
        assert progress["peak_rss_bytes"] == 2048
        assert progress["peak_cpu_percent"] == 40.0

    async def test_last_attempt_progress_is_null_by_default(self, client, two_profiles):
        owner = two_profiles["a"].owner_id()
        job = await _make_job(owner)

        r = await client.get(
            f"/api/v1/jobs/{job.id}", headers=two_profiles["a_headers"]
        )

        assert r.json()["last_attempt_progress"] is None

    async def test_last_attempt_progress_surfaces_when_present(self, client, two_profiles):
        owner = two_profiles["a"].owner_id()
        job = await _make_job(
            owner,
            attempts=2,
            last_attempt_progress=AttemptProgress(
                attempt=1, pct=0.8, phase="assembling", peak_rss_bytes=15_000_000_000
            ),
        )

        r = await client.get(
            f"/api/v1/jobs/{job.id}", headers=two_profiles["a_headers"]
        )

        last = r.json()["last_attempt_progress"]
        assert last is not None
        assert last["attempt"] == 1
        assert last["pct"] == 0.8
        assert last["phase"] == "assembling"
        assert last["peak_rss_bytes"] == 15_000_000_000

    async def test_progress_fields_also_appear_on_the_list_endpoint(
        self, client, two_profiles
    ):
        owner = two_profiles["a"].owner_id()
        await _make_job(owner, progress=JobProgress(pct=None, phase="starting"))

        r = await client.get("/api/v1/jobs", headers=two_profiles["a_headers"])

        assert r.status_code == 200
        jobs = [j for j in r.json() if j["type"] == "jobs_progress_probe"]
        assert jobs
        assert jobs[0]["progress"]["pct"] is None
        assert jobs[0]["progress"]["phase"] == "starting"


class TestEtaSecondsOnJobDetail:
    async def test_eta_extrapolates_from_started_at_and_pct(self, client, two_profiles):
        owner = two_profiles["a"].owner_id()
        job = await _make_job(
            owner,
            state=JobState.RUNNING,
            progress=JobProgress(pct=0.5),
        )
        job.timing.started_at = datetime.now(UTC) - timedelta(seconds=100)
        await job.save()

        r = await client.get(
            f"/api/v1/jobs/{job.id}", headers=two_profiles["a_headers"]
        )

        assert r.json()["eta_seconds"] == pytest.approx(100.0, rel=0.05)

    async def test_no_eta_key_when_pct_and_history_are_both_absent(
        self, client, two_profiles
    ):
        owner = two_profiles["a"].owner_id()
        job = await _make_job(
            owner,
            state=JobState.RUNNING,
            progress=JobProgress(pct=None, phase="starting"),
        )
        job.timing.started_at = datetime.now(UTC)
        await job.save()

        r = await client.get(
            f"/api/v1/jobs/{job.id}", headers=two_profiles["a_headers"]
        )

        assert "eta_seconds" not in r.json()

    async def test_no_eta_for_a_terminal_job(self, client, two_profiles):
        """Only worth predicting while the job is still running -- afterwards
        the actual duration is the better number, same rule timing_estimate
        already follows."""
        owner = two_profiles["a"].owner_id()
        job = await _make_job(
            owner,
            state=JobState.SUCCEEDED,
            progress=JobProgress(pct=1.0),
        )
        job.timing.started_at = datetime.now(UTC) - timedelta(seconds=100)
        await job.save()

        r = await client.get(
            f"/api/v1/jobs/{job.id}", headers=two_profiles["a_headers"]
        )

        assert "eta_seconds" not in r.json()
