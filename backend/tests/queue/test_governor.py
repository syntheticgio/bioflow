"""Admission control under synthetic load traces.

The point of these tests is the property that is hard to get right and easy to
regress: the governor must not oscillate. A sawtooth CPU trace that crosses the
threshold repeatedly should produce a handful of state changes, not one per
sample.

A fake clock is injected throughout -- no sleep() anywhere, so the suite stays
fast and deterministic.
"""

import inspect

import pytest
from app.models import JobClass
from app.queue.governor import (
    CPU_CLOSE,
    CPU_OPEN,
    CPU_THROTTLE,
    DWELL_SECONDS,
    RAMP_COUNT,
    RAMP_INTERVAL,
    SAMPLE_INTERVAL,
    AdmissionState,
    LoadGovernor,
    LoadSample,
)
from app.queue.worker import Worker


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def feed(gov: LoadGovernor, clock: FakeClock, cpu_trace, *, mem=40.0, step=SAMPLE_INTERVAL):
    """Drive the governor with a CPU trace, returning the state after each step.

    Metrics are injected directly rather than sampled: the state machine is what
    is under test, not psutil.
    """
    states = []
    for cpu in cpu_trace:
        clock.advance(step)
        gov.ewma = LoadSample(
            cpu_percent=cpu,
            mem_percent=mem,
            mem_available=32 * 1024**3,
            load1=cpu / 100.0,
            swap_in_mb_s=0.0,
            disk_free_bytes=500 * 1024**3,
            disk_free_percent=50.0,
            at=clock.now,
        )
        states.append(gov.evaluate())
    return states


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def gov(clock, monkeypatch):
    g = LoadGovernor(clock=clock)
    # Fixed budgets so thresholds are not host-dependent.
    monkeypatch.setattr(g, "cpu_budget", lambda: 8.0)
    monkeypatch.setattr(g, "mem_budget_bytes", lambda: 64 * 1024**3)
    return g


class TestBasicTransitions:
    def test_starts_open(self, gov):
        assert gov.state is AdmissionState.OPEN

    def test_closes_under_sustained_high_cpu(self, gov, clock):
        feed(gov, clock, [95.0] * 20)
        assert gov.state is AdmissionState.CLOSED

    def test_throttles_in_the_middle_band(self, gov, clock):
        feed(gov, clock, [(CPU_THROTTLE + CPU_CLOSE) / 2] * 20)
        assert gov.state is AdmissionState.THROTTLED

    def test_reopens_once_load_clears(self, gov, clock):
        feed(gov, clock, [95.0] * 20)
        assert gov.state is AdmissionState.CLOSED
        feed(gov, clock, [10.0] * 40)
        assert gov.state is AdmissionState.OPEN


class TestHysteresis:
    def test_does_not_reopen_inside_the_dead_band(self, gov, clock):
        """Between the close and open thresholds the governor holds its state.

        Reopening here is exactly what causes flapping: the load that closed it
        has not actually gone away.
        """
        feed(gov, clock, [95.0] * 20)
        assert gov.state is AdmissionState.CLOSED

        midband = (CPU_OPEN + CPU_CLOSE) / 2
        feed(gov, clock, [midband] * 40)
        assert gov.state is AdmissionState.CLOSED

    def test_close_and_open_thresholds_are_well_separated(self):
        assert CPU_CLOSE - CPU_OPEN >= 10.0


class TestNoOscillation:
    def test_sawtooth_across_the_threshold_produces_few_transitions(self, gov, clock):
        """The regression this whole design exists to prevent.

        A trace flipping either side of the close threshold every sample would,
        without hysteresis and dwell, change state on every step.
        """
        trace = [95.0, 60.0] * 40  # 80 samples, crossing every time
        states = feed(gov, clock, trace)

        transitions = sum(1 for a, b in zip(states, states[1:], strict=False) if a != b)
        # 80 samples at 2s = 160s of trace; a 20s dwell caps changes at ~8.
        assert transitions <= 8, f"{transitions} transitions -- governor is flapping"

    def test_dwell_blocks_immediate_reversal(self, gov, clock):
        # A single sample is enough to close, and leaves the dwell window open --
        # feeding a long trace here would let dwell expire and test nothing.
        feed(gov, clock, [95.0])
        assert gov.state is AdmissionState.CLOSED
        changed_at = gov._state_changed_at

        # Load vanishes immediately, but not enough time has passed to act on it.
        clock.advance(DWELL_SECONDS / 4)
        gov.ewma = LoadSample(
            cpu_percent=5.0, mem_percent=20.0, mem_available=60 * 1024**3,
            load1=0.05, disk_free_bytes=500 * 1024**3, disk_free_percent=50.0,
        )
        assert gov.evaluate() is AdmissionState.CLOSED
        assert gov._state_changed_at == changed_at

        # Once the window expires, the pending change is applied.
        clock.advance(DWELL_SECONDS)
        assert gov.evaluate() is AdmissionState.OPEN

    def test_spike_shorter_than_dwell_does_not_flap(self, gov, clock):
        feed(gov, clock, [10.0] * 20)
        assert gov.state is AdmissionState.OPEN

        # One brief spike, then calm again.
        states = feed(gov, clock, [99.0, 99.0] + [10.0] * 30)
        assert states[-1] is AdmissionState.OPEN
        transitions = sum(1 for a, b in zip(states, states[1:], strict=False) if a != b)
        assert transitions <= 2


class TestUserInteractiveNeverBlocked:
    @pytest.mark.parametrize("state", list(AdmissionState))
    def test_admitted_in_every_state(self, gov, state):
        """A user clicking a button must always get a response. A UI that goes
        dead under load is worse than a briefly oversubscribed machine."""
        gov.state = state
        assert JobClass.USER_INTERACTIVE.value in gov.allowed_classes()

    def test_closed_admits_only_user_interactive(self, gov):
        gov.state = AdmissionState.CLOSED
        assert gov.allowed_classes() == [JobClass.USER_INTERACTIVE.value]

    def test_throttled_defers_maintenance_and_bulk(self, gov):
        gov.state = AdmissionState.THROTTLED
        allowed = gov.allowed_classes()
        assert JobClass.MAINTENANCE.value not in allowed
        assert JobClass.BULK.value not in allowed
        assert JobClass.USER_BACKGROUND.value in allowed


class TestComputeAdmission:
    def test_admitted_only_when_open(self, gov):
        """A pipeline run is the heaviest thing the system does, so it is the
        first work shed when the machine is under any strain."""
        gov.state = AdmissionState.OPEN
        assert JobClass.COMPUTE.value in gov.allowed_classes()

        for state in (AdmissionState.THROTTLED, AdmissionState.CLOSED):
            gov.state = state
            assert JobClass.COMPUTE.value not in gov.allowed_classes(), state

    def test_starvation_escape_is_limited_to_maintenance(self):
        """Compute must not inherit the escape hatch.

        The 30-minute override exists because a verify_files that never runs
        fails *silently*. A waiting pipeline run is visible as waiting in the
        activity view, and forcing a multi-hour job onto an already-strained
        machine is precisely what the governor is for. Asserted against the
        source because the alternative -- generalizing the escape to "any
        deferred class" -- is a plausible future edit that reads as a cleanup.
        """
        source = inspect.getsource(Worker._try_claim)
        assert "JobClass.MAINTENANCE.value" in source
        assert "JobClass.COMPUTE" not in source


class TestRampUp:
    def test_reopening_starts_a_ramp(self, gov, clock):
        feed(gov, clock, [95.0] * 20)
        feed(gov, clock, [10.0] * 40)
        assert gov.state is AdmissionState.OPEN
        assert gov._ramp_started_at is not None

    def test_ramp_rate_limits_admissions(self, gov, clock):
        feed(gov, clock, [95.0] * 20)
        feed(gov, clock, [10.0] * 40)

        assert gov.may_admit_now() is True
        gov.record_admission()
        # Immediately after, the bucket is empty.
        assert gov.may_admit_now() is False
        clock.advance(RAMP_INTERVAL + 0.1)
        assert gov.may_admit_now() is True

    def test_ramp_ends_after_enough_clean_admissions(self, gov, clock):
        feed(gov, clock, [95.0] * 20)
        feed(gov, clock, [10.0] * 40)

        for _ in range(RAMP_COUNT):
            clock.advance(RAMP_INTERVAL + 0.1)
            assert gov.may_admit_now()
            gov.record_admission()

        assert gov._ramp_started_at is None
        # Back to normal: no rate limit between admissions.
        assert gov.may_admit_now() is True

    def test_no_rate_limit_when_not_ramping(self, gov):
        assert gov._ramp_started_at is None
        for _ in range(10):
            assert gov.may_admit_now() is True
            gov.record_admission()


class TestOtherPressureSignals:
    def _metrics(self, **over):
        base = dict(
            cpu_percent=10.0, mem_percent=30.0, mem_available=32 * 1024**3,
            load1=0.1, swap_in_mb_s=0.0,
            disk_free_bytes=500 * 1024**3, disk_free_percent=50.0,
        )
        base.update(over)
        return LoadSample(**base)

    def test_memory_pressure_closes(self, gov, clock):
        clock.advance(DWELL_SECONDS + 1)
        gov.ewma = self._metrics(mem_percent=95.0)
        assert gov.evaluate() is AdmissionState.CLOSED

    def test_swap_thrashing_closes(self, gov, clock):
        """The strongest real signal that a machine is in trouble."""
        clock.advance(DWELL_SECONDS + 1)
        gov.ewma = self._metrics(swap_in_mb_s=50.0)
        assert gov.evaluate() is AdmissionState.CLOSED

    def test_low_disk_closes(self, gov, clock):
        clock.advance(DWELL_SECONDS + 1)
        gov.ewma = self._metrics(disk_free_percent=2.0, disk_free_bytes=1 * 1024**3)
        assert gov.evaluate() is AdmissionState.CLOSED

    def test_high_load_average_closes(self, gov, clock):
        clock.advance(DWELL_SECONDS + 1)
        gov.ewma = self._metrics(load1=3.0)
        assert gov.evaluate() is AdmissionState.CLOSED

    def test_unreadable_disk_is_not_treated_as_full(self, gov, clock):
        """disk_free_bytes == 0 means we could not stat the mount, which is the
        storage layer's problem to report -- not a reason to stop all work."""
        clock.advance(DWELL_SECONDS + 1)
        gov.ewma = self._metrics(disk_free_bytes=0, disk_free_percent=100.0)
        assert gov.evaluate() is AdmissionState.OPEN


class TestEwmaSmoothing:
    def test_single_spike_does_not_move_the_average_far(self, gov, monkeypatch):
        """Decisions follow the trend, never one instantaneous reading."""
        import psutil

        monkeypatch.setattr(psutil, "cpu_percent", lambda interval=None: 10.0)
        gov._primed = True
        for _ in range(10):
            gov.sample()
        calm = gov.ewma.cpu_percent

        monkeypatch.setattr(psutil, "cpu_percent", lambda interval=None: 100.0)
        gov.sample()

        # One 100% sample must not drag an EWMA of ~10 past the close threshold.
        assert gov.ewma.cpu_percent < CPU_CLOSE
        assert gov.ewma.cpu_percent > calm

    def test_first_sample_is_not_a_false_zero(self, gov, monkeypatch):
        """psutil.cpu_percent(interval=None) returns 0.0 on its first call."""
        import psutil

        monkeypatch.setattr(psutil, "cpu_percent", lambda interval=None: 0.0)
        gov._primed = False
        gov.sample()
        assert gov._primed is True


class TestSnapshot:
    def test_reports_active_and_serializes(self, gov, clock):
        feed(gov, clock, [50.0] * 5)
        snap = gov.snapshot()
        assert snap["governor_active"] is True
        assert snap["state"] in {s.value for s in AdmissionState}
        assert "cpu" in snap and "memory" in snap
        import json

        json.dumps(snap)  # must survive the wire
