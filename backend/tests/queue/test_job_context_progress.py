"""JobContext.progress(): what reaches the callback, and what None means.

Handlers whose tool cannot produce an honest fraction (Flye, Clair3, minimap2
-- see assembly_runner.py:83) call `ctx.progress(pct=None, ...)` meaning
"unknown", not "unchanged". Before this test, `pct=None` was filtered out of
the update dict along with every other omitted field, so the call site's
intent was silently discarded and the job kept whatever pct it last had --
0.0 on a first call, which renders as a bar stuck at zero for the run's
entire life.
"""

from app.queue.registry import JobContext


def _ctx(calls: list[dict]) -> JobContext:
    return JobContext(
        job_id="j1",
        payload={},
        epoch=0,
        attempts=0,
        owner="local",
        _progress_cb=calls.append,
    )


class TestPctNoneIsExplicit:
    def test_pct_none_reaches_the_callback(self):
        """The regression: a phase-only handler reporting pct=None must have
        that null actually written, not silently dropped."""
        calls: list[dict] = []
        ctx = _ctx(calls)

        ctx.progress(phase="starting", pct=None, message="starting flye")

        assert calls == [{"phase": "starting", "pct": None, "message": "starting flye"}]

    def test_a_later_call_can_clear_a_known_pct(self):
        """A tool that had a fraction and then loses it (falls back to a
        phase-only tail) must be able to say so explicitly."""
        calls: list[dict] = []
        ctx = _ctx(calls)

        ctx.progress(pct=0.5)
        ctx.progress(phase="finalizing", pct=None)

        assert calls[-1] == {"phase": "finalizing", "pct": None}

    def test_omitting_pct_entirely_still_omits_it(self):
        """Not passing pct at all is different from passing pct=None: the
        first must not overwrite whatever pct currently holds."""
        calls: list[dict] = []
        ctx = _ctx(calls)

        ctx.progress(phase="hashing")

        assert calls == [{"phase": "hashing"}]
        assert "pct" not in calls[0]

    def test_other_fields_are_unaffected_by_the_pct_special_case(self):
        """phase/message/bytes_* keep the existing omit-means-unchanged
        behaviour; only pct gets the explicit-None carve-out."""
        calls: list[dict] = []
        ctx = _ctx(calls)

        ctx.progress(phase=None, message=None, bytes_done=None, bytes_total=None)

        assert calls == []

    def test_no_callback_is_a_silent_noop_even_with_pct_none(self):
        ctx = JobContext(job_id="j1", payload={}, epoch=0, attempts=0, owner="local")
        ctx.progress(pct=None, phase="starting")  # must not raise


class TestGenericUnits:
    def test_units_reach_the_callback(self):
        """bytes_* stays for hashing/chunk assembly; units_* is for anything
        countable that isn't a size -- chunks, reads, contigs, records."""
        calls: list[dict] = []
        ctx = _ctx(calls)

        ctx.progress(units_done=3, units_total=7, unit_label="chunks")

        assert calls == [{"units_done": 3, "units_total": 7, "unit_label": "chunks"}]

    def test_units_round_trip_through_the_job_document(self):
        from app.models.job import JobProgress

        progress = JobProgress(units_done=3, units_total=7, unit_label="chunks")
        dumped = progress.model_dump(mode="json")

        assert dumped["units_done"] == 3
        assert dumped["units_total"] == 7
        assert dumped["unit_label"] == "chunks"


class TestPhaseStructure:
    def test_phase_index_and_total_reach_the_callback(self):
        calls: list[dict] = []
        ctx = _ctx(calls)

        ctx.progress(phase="trimming", phase_index=1, phase_total=3)

        assert calls == [{"phase": "trimming", "phase_index": 1, "phase_total": 3}]

    def test_omitted_phase_structure_is_absent_not_null(self):
        """A parser with no stage order to report from (e.g. a
        default-constructed AssemblyProgress with no `stage_order`) must not
        have to pass phase_index=None explicitly on every call -- omitting
        the kwargs is enough."""
        calls: list[dict] = []
        ctx = _ctx(calls)

        ctx.progress(phase="configuring")

        assert "phase_index" not in calls[0]
        assert "phase_total" not in calls[0]

    def test_phase_structure_round_trips_through_the_job_document(self):
        from app.models.job import JobProgress

        progress = JobProgress(phase="trimming", phase_index=1, phase_total=3)
        dumped = progress.model_dump(mode="json")

        assert dumped["phase_index"] == 1
        assert dumped["phase_total"] == 3

    def test_default_phase_structure_is_null(self):
        from app.models.job import JobProgress

        dumped = JobProgress().model_dump(mode="json")

        assert dumped["phase_index"] is None
        assert dumped["phase_total"] is None
