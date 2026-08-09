"""The Reference → Metrics view: what computations have cost.

The page reads `timing_service.metrics()`, which is a diagnostics view, not a
model input. Two things about it are load-bearing and worth pinning down:

  * Outcome counts include failed runs (a metrics page that hid failures
    would be a status page for a rosier app), while every duration / memory /
    size summary reads only successful runs through the same `_modelled`
    accessor the predictive models use.
  * Unmeasured values are `None`, never 0 -- a run under the 60s resource
    floor did not measure zero memory, it measured nothing.
"""

import pytest

from app.models.timing import JobRunTiming, RunOutcome, RunResources
from app.services import timing_service


class TestPercentiles:
    def test_empty_is_none(self):
        assert timing_service._percentile([], 50) is None
        assert timing_service._summary([]) == {"median": None, "p90": None}

    def test_median_of_odd_count(self):
        assert timing_service._percentile([10, 20, 30, 40, 50], 50) == 30

    def test_median_of_even_count(self):
        # Nearest-rank: the lower middle value. Determinism over convention.
        assert timing_service._percentile([10, 20, 30, 40], 50) == 20

    def test_p90_is_at_or_below_the_max(self):
        values = list(range(1, 101))
        assert timing_service._percentile(values, 90) == 90

    def test_p90_single_value(self):
        assert timing_service._percentile([7], 90) == 7


class TestToolCounts:
    def test_most_used_first(self):
        records = [
            JobRunTiming(job_type="qc", input_bytes=1, duration_ms=1, tool="fastp", tool_version="0.24.0"),
            JobRunTiming(job_type="qc", input_bytes=1, duration_ms=1, tool="FastQC", tool_version="v0.12.1"),
            JobRunTiming(job_type="qc", input_bytes=1, duration_ms=1, tool="fastp", tool_version="0.24.0"),
        ]
        counts = timing_service._tool_counts(records)
        assert counts[0] == {"name": "fastp", "version": "0.24.0", "runs": 2}
        assert counts[1]["runs"] == 1

    def test_unrecorded_tool_is_named_null_not_dropped(self):
        records = [JobRunTiming(job_type="qc", input_bytes=1, duration_ms=1, tool=None)]
        assert timing_service._tool_counts(records) == [{"name": None, "version": None, "runs": 1}]


class TestNumericFeatures:
    def test_only_positive_ints_count(self):
        records = [
            JobRunTiming(job_type="qc", input_bytes=1, duration_ms=1, features={"read_count": 1000}),
            JobRunTiming(job_type="qc", input_bytes=1, duration_ms=1, features={"read_count": 0}),
            JobRunTiming(job_type="qc", input_bytes=1, duration_ms=1, features={"read_count": "many"}),
            JobRunTiming(job_type="qc", input_bytes=1, duration_ms=1, features={}),
        ]
        assert timing_service._numeric_features(records, "read_count") == [1000]


@pytest.fixture(autouse=True)
async def _fresh_job_timings():
    """Real inserts against `JobRunTiming`, isolated per test.

    Same pattern as tests/queue/test_record_outcomes.py: these tests assert
    exact counts, so leftover rows from a sibling test would corrupt them.
    """
    from beanie import init_beanie
    from motor.motor_asyncio import AsyncIOMotorClient

    from app.config import settings
    from app.models import ALL_MODELS

    client = AsyncIOMotorClient(settings.mongo_url, tz_aware=True)
    db = client["biopipe_test"]
    await db[JobRunTiming.Settings.name].drop()
    await init_beanie(database=db, document_models=ALL_MODELS)
    yield
    client.close()


async def _record(
    *,
    job_type="align_reads",
    outcome=RunOutcome.SUCCEEDED,
    duration_ms=120_000,
    input_bytes=1_000_000,
    peak_rss_bytes=None,
    tool="minimap2",
    tool_version="2.28",
    features=None,
):
    resources = RunResources()
    if peak_rss_bytes is not None:
        resources.peak_rss_bytes = peak_rss_bytes
    await JobRunTiming(
        job_type=job_type,
        input_bytes=input_bytes,
        duration_ms=duration_ms,
        outcome=outcome,
        tool=tool,
        tool_version=tool_version,
        features=features or {},
        resources=resources,
    ).insert()


class TestMetrics:
    async def test_outcome_counts_include_failures(self):
        for _ in range(6):
            await _record()
        await _record(outcome=RunOutcome.FAILED, duration_ms=500)
        await _record(outcome=RunOutcome.DEAD, duration_ms=100)

        data = await timing_service.metrics()
        assert data["totals"]["succeeded"] == 6
        assert data["totals"]["failed"] == 1
        assert data["totals"]["dead"] == 1

        row = next(t for t in data["types"] if t["job_type"] == "align_reads")
        assert row["outcomes"]["succeeded"] == 6
        assert row["outcomes"]["failed"] == 1

    async def test_duration_summaries_read_successes_only(self):
        # Five successes at 100s, one failure at 1s -- the failure must not
        # drag the median down to something that looks like a fast run.
        for _ in range(5):
            await _record(duration_ms=100_000)
        await _record(outcome=RunOutcome.FAILED, duration_ms=1_000)

        row = next(t for t in (await timing_service.metrics())["types"] if t["job_type"] == "align_reads")
        assert row["duration_ms"]["median"] == 100_000
        assert row["duration_ms"]["p90"] == 100_000

    async def test_memory_only_counts_measured_runs(self):
        # Three runs above the floor, one under (peak_rss None). The median
        # must come from the measured three, and an unmeasured run must not
        # read as zero bytes.
        await _record(peak_rss_bytes=1_000_000_000)
        await _record(peak_rss_bytes=3_000_000_000)
        await _record(peak_rss_bytes=5_000_000_000)
        await _record(peak_rss_bytes=None)

        row = next(t for t in (await timing_service.metrics())["types"] if t["job_type"] == "align_reads")
        assert row["peak_rss_bytes"]["median"] == 3_000_000_000

    async def test_read_count_and_input_bytes_are_summarized(self):
        await _record(input_bytes=500_000, features={"read_count": 1000})
        await _record(input_bytes=1_500_000, features={"read_count": 3000})
        await _record(input_bytes=1_000_000, features={"read_count": 2000})
        await _record(input_bytes=2_000_000, features={})
        await _record(input_bytes=2_500_000, features={})

        row = next(t for t in (await timing_service.metrics())["types"] if t["job_type"] == "align_reads")
        assert row["input_bytes"]["median"] == 1_500_000
        # Only the three runs that recorded a read count contribute -- the
        # unrecorded fourth is a missing measurement, not a count of zero.
        assert row["read_count"]["median"] == 2000

    async def test_tools_break_down_by_name_and_version(self):
        await _record(tool="minimap2", tool_version="2.28")
        await _record(tool="minimap2", tool_version="2.28")
        await _record(tool="minimap2", tool_version="2.26")

        row = next(t for t in (await timing_service.metrics())["types"] if t["job_type"] == "align_reads")
        assert row["tools"][0] == {"name": "minimap2", "version": "2.28", "runs": 2}
        assert row["tools"][1] == {"name": "minimap2", "version": "2.26", "runs": 1}

    async def test_rows_recorded_before_the_outcome_field_count_as_succeeded(self):
        # The real collection holds rows recorded before the outcome field
        # existed (2026-08-03), carrying no `outcome` at all. They were
        # written on the success path only, so the metrics view counts them
        # as succeeded -- not as a mysterious fourth bucket. The duration
        # summaries leave them out, since those need a recorded success
        # outcome: counts cover all history, summaries cover the window.
        col = JobRunTiming.get_pymongo_collection()
        await col.insert_one(
            {
                "job_type": "legacy_thing",
                "input_bytes": 123,
                "duration_ms": 456,
            }
        )

        data = await timing_service.metrics()
        assert data["totals"]["succeeded"] == 1
        assert "failed" not in data["totals"]
        row = next(t for t in data["types"] if t["job_type"] == "legacy_thing")
        assert row["outcomes"] == {"succeeded": 1}
        assert row["duration_ms"] == {"median": None, "p90": None}

    async def test_empty_history_is_a_clean_empty_response(self):
        data = await timing_service.metrics()
        assert data == {"totals": {}, "types": []}
