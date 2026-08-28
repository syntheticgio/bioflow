"""The worker's liveness file and the healthcheck that reads it (#878).

The worker was the one service with no healthcheck, so it reported "Up" while
wedged and nothing noticed -- including the launcher's health panel, which
reads compose healthchecks.
"""

import time

import pytest

from app.config import settings
from app.queue import liveness


@pytest.fixture(autouse=True)
def liveness_file(tmp_path, monkeypatch):
    monkeypatch.setattr(liveness, "LIVENESS_PATH", tmp_path / "alive")


class TestFreshness:
    def test_a_worker_that_never_beat_is_not_healthy(self):
        """No file at all. compose's start_period covers legitimate startup;
        reporting healthy before the first beat would defeat the check."""
        assert liveness.is_alive() is False

    def test_a_just_touched_file_is_healthy(self):
        liveness.touch_liveness()
        assert liveness.is_alive() is True

    def test_a_stale_file_is_not_healthy(self):
        liveness.touch_liveness()
        # One second past the threshold, without waiting for it.
        future = time.time() + liveness.staleness_threshold_seconds() + 1
        assert liveness.is_alive(now=future) is False

    def test_a_file_just_inside_the_threshold_is_still_healthy(self):
        """The boundary matters: a single slow beat under load must not restart
        a working worker."""
        liveness.touch_liveness()
        edge = time.time() + liveness.staleness_threshold_seconds() - 1
        assert liveness.is_alive(now=edge) is True

    def test_one_missed_beat_is_tolerated(self):
        """Three beats' grace, not one. A restart is a heavier remedy than
        waiting one more interval."""
        liveness.touch_liveness()
        one_beat_late = time.time() + liveness.heartbeat_interval_seconds() * 1.5
        assert liveness.is_alive(now=one_beat_late) is True


class TestThresholdTracksTheHeartbeat:
    """The threshold is derived from lease_ttl_seconds rather than fixed,
    because the heartbeat interval is -- a deployment that raised the lease TTL
    would otherwise start failing its own healthcheck."""

    def test_the_interval_matches_the_heartbeat_loop_formula(self, monkeypatch):
        monkeypatch.setattr(settings, "lease_ttl_seconds", 30)
        assert liveness.heartbeat_interval_seconds() == 10
        monkeypatch.setattr(settings, "lease_ttl_seconds", 300)
        assert liveness.heartbeat_interval_seconds() == 100

    def test_the_interval_has_a_floor(self, monkeypatch):
        """A very short lease must not produce a sub-second probe budget."""
        monkeypatch.setattr(settings, "lease_ttl_seconds", 1)
        assert liveness.heartbeat_interval_seconds() == 2

    def test_a_longer_lease_widens_the_threshold(self, monkeypatch):
        monkeypatch.setattr(settings, "lease_ttl_seconds", 300)
        liveness.touch_liveness()
        # 90s late would be dead at the default TTL, alive at this one.
        assert liveness.is_alive(now=time.time() + 90) is True


class TestTouchNeverRaises:
    def test_an_unwritable_path_does_not_kill_the_heartbeat(self, monkeypatch, tmp_path):
        """A healthcheck that cannot be written is not a reason to stop a
        worker that is otherwise doing its job. It shows up as staleness,
        which is the same signal by a slower route."""
        monkeypatch.setattr(
            liveness, "LIVENESS_PATH", tmp_path / "no-such-dir" / "alive"
        )
        liveness.touch_liveness()  # must not raise
        assert liveness.is_alive() is False


class TestProbeExitCode:
    def test_exits_zero_when_alive(self):
        liveness.touch_liveness()
        assert liveness.main() == 0

    def test_exits_nonzero_when_wedged(self):
        assert liveness.main() == 1


def test_the_heartbeat_loop_touches_the_file():
    """Ties the probe to the thing it measures. The import is what matters:
    if _heartbeat_loop stopped calling touch_liveness, the healthcheck would
    fail every worker rather than only wedged ones -- loudly, but for the
    wrong reason."""
    import inspect

    from app.queue.worker import Worker

    source = inspect.getsource(Worker._heartbeat_loop)
    assert "touch_liveness()" in source
