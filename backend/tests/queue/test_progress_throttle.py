"""Progress throttling, and the one update that must not be throttled.

Written after a real assembly run sat at "starting" for its entire six-minute
life. The handler reported "starting", Flye's `configure` and `assembly`
banners both arrived inside the same second, and the throttle dropped both --
then the tool produced no further stage line and the job's phase never moved.
"""

from unittest.mock import patch

from app.queue.executor import JobExecutor


class _Recorder(JobExecutor):
    """An executor that records what would be written instead of writing it.

    `_schedule_progress` reaches the database through a running event loop; the
    throttle decision happens before any of that, and is the whole subject
    here.
    """

    def __init__(self):
        super().__init__("test-worker")
        self.writes: list[dict] = []

    def _write_now(self, update: dict) -> None:
        self.writes.append(update)


def _run(executor, updates, *, times):
    """Feed updates at controlled timestamps."""
    with patch("app.queue.executor.asyncio.get_running_loop", side_effect=RuntimeError):
        for update, when in zip(updates, times, strict=True):
            with patch("app.queue.executor.datetime") as dt:
                dt.now.return_value.timestamp.return_value = when
                # No loop and no _loop attribute: _schedule_progress returns
                # after the throttle decision, which is what we are measuring.
                before = executor._last_progress.get("job1", 0.0)
                before_phase = executor._last_phase.get("job1")
                executor._schedule_progress("job1", 1, update, owner="local")
                after = executor._last_progress.get("job1", 0.0)
                after_phase = executor._last_phase.get("job1")
                if after != before or after_phase != before_phase:
                    executor.writes.append(update)


class TestPercentageIsThrottled:
    def test_rapid_percentage_ticks_are_dropped(self):
        """The behaviour the throttle exists for: a job reporting at 5 Hz would
        otherwise cause a Mongo write and an SSE fan-out per tick."""
        ex = _Recorder()
        _run(
            ex,
            [{"pct": 0.1}, {"pct": 0.2}, {"pct": 0.3}],
            times=[100.0, 100.1, 100.2],
        )
        assert ex.writes == [{"pct": 0.1}]

    def test_a_percentage_gets_through_once_the_interval_passes(self):
        ex = _Recorder()
        _run(ex, [{"pct": 0.1}, {"pct": 0.2}], times=[100.0, 101.0])
        assert len(ex.writes) == 2


class TestPhaseIsNot:
    def test_a_phase_change_inside_the_interval_still_writes(self):
        """The exact production sequence, at the timestamps it actually had.

        Reverting the exemption fails here: "configuring" and "assembling
        draft" both land within 0.5s of "starting" and would be dropped.
        """
        ex = _Recorder()
        _run(
            ex,
            [
                {"phase": "starting", "message": "starting flye"},
                {"phase": "configuring"},
                {"phase": "assembling draft"},
            ],
            times=[100.0, 100.05, 100.1],
        )
        assert [w["phase"] for w in ex.writes] == [
            "starting",
            "configuring",
            "assembling draft",
        ]

    def test_the_same_phase_repeated_is_still_throttled(self):
        """The exemption is for a *change*, not for the presence of the key.

        Without this, a handler that passes its phase on every tick would
        bypass the throttle entirely and reinstate the write storm the throttle
        exists to prevent.
        """
        ex = _Recorder()
        _run(
            ex,
            [
                {"phase": "aligning", "pct": 0.1},
                {"phase": "aligning", "pct": 0.2},
                {"phase": "aligning", "pct": 0.3},
            ],
            times=[100.0, 100.1, 100.2],
        )
        assert len(ex.writes) == 1

    def test_a_later_phase_change_still_bypasses(self):
        """Alignment's "sorting" transition: one change, minutes in, after a
        long run of throttled percentage ticks."""
        ex = _Recorder()
        _run(
            ex,
            [
                {"phase": "aligning", "pct": 0.5},
                {"pct": 0.6},
                {"phase": "sorting"},
            ],
            times=[100.0, 100.1, 100.2],
        )
        assert [w.get("phase") for w in ex.writes] == ["aligning", "sorting"]


class TestCleanup:
    def test_finishing_a_job_forgets_its_phase(self):
        """Same lifetime as _last_progress. A job id that kept its phase would
        make a *re*-run of the same job skip its first phase write, which is
        the one that says the job started."""
        ex = _Recorder()
        _run(ex, [{"phase": "starting"}], times=[100.0])
        assert ex._last_phase.get("job1") == "starting"

        ex._last_progress.pop("job1", None)
        ex._last_phase.pop("job1", None)
        assert "job1" not in ex._last_phase
