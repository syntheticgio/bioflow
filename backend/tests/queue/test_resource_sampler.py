"""Peak resource sampling over a process subtree.

Tested against fakes rather than real processes: the arithmetic (max
retention, subtree summing, tolerance of processes that vanish mid-walk) is
what can be wrong, and spawning real children would make the test slow and
flaky without exercising anything more.
"""

import pytest
from app.queue.resource_sampler import ResourceSampler


class FakeProcess:
    """Stands in for psutil.Process."""

    def __init__(self, rss, cpu, children=None, gone=False, pid=None):
        self._rss = rss
        self._cpu = cpu
        self._children = children or []
        self._gone = gone
        # Real psutil.Process objects always have a distinct .pid; default to
        # object identity so fakes that don't care about pid collisions (most
        # existing tests) don't accidentally alias each other under one key.
        self.pid = pid if pid is not None else id(self)
        self.cpu_percent_calls = 0

    def memory_info(self):
        if self._gone:
            raise ProcessLookupError("process is gone")
        return type("MemInfo", (), {"rss": self._rss})()

    def cpu_percent(self, interval=None):
        if self._gone:
            raise ProcessLookupError("process is gone")
        self.cpu_percent_calls += 1
        return self._cpu

    def children(self, recursive=True):
        if self._gone:
            raise ProcessLookupError("process is gone")
        return self._children


class TestPeakRetention:
    def test_retains_the_maximum_not_the_last(self):
        """A peak is a high-water mark; a later smaller reading must not
        overwrite it."""
        sampler = ResourceSampler(pid=1)
        sampler.observe(FakeProcess(rss=900, cpu=10.0))
        sampler.observe(FakeProcess(rss=100, cpu=5.0))
        assert sampler.peak_rss_bytes == 900
        assert sampler.peak_cpu_percent == 10.0

    def test_mean_cpu_averages_every_sample(self):
        sampler = ResourceSampler(pid=1)
        sampler.observe(FakeProcess(rss=10, cpu=20.0))
        sampler.observe(FakeProcess(rss=10, cpu=40.0))
        assert sampler.mean_cpu_percent == pytest.approx(30.0)

    def test_sample_count_tracks_observations(self):
        sampler = ResourceSampler(pid=1)
        for _ in range(3):
            sampler.observe(FakeProcess(rss=10, cpu=1.0))
        assert sampler.sample_count == 3


class TestSubtree:
    def test_sums_children_into_the_total(self):
        """A tool that forks workers must be measured as one job, not as the
        parent's own small footprint."""
        children = [FakeProcess(rss=300, cpu=25.0), FakeProcess(rss=200, cpu=15.0)]
        sampler = ResourceSampler(pid=1)
        sampler.observe(FakeProcess(rss=100, cpu=10.0, children=children))
        assert sampler.peak_rss_bytes == 600
        assert sampler.peak_cpu_percent == pytest.approx(50.0)

    def test_a_child_that_vanishes_does_not_lose_the_sample(self):
        """Processes exit mid-walk constantly. The surviving members still
        produce a usable number."""
        children = [FakeProcess(rss=300, cpu=25.0), FakeProcess(rss=999, cpu=99.0, gone=True)]
        sampler = ResourceSampler(pid=1)
        sampler.observe(FakeProcess(rss=100, cpu=10.0, children=children))
        assert sampler.peak_rss_bytes == 400
        assert sampler.sample_count == 1

    def test_root_vanishing_records_no_sample(self):
        sampler = ResourceSampler(pid=1)
        sampler.observe(FakeProcess(rss=0, cpu=0.0, gone=True))
        assert sampler.sample_count == 0
        assert sampler.peak_rss_bytes is None


class TestEmptyState:
    def test_no_observations_yields_none_not_zero(self):
        """Zero would be a measurement. None is the absence of one, and the
        model must be able to tell them apart."""
        sampler = ResourceSampler(pid=1)
        assert sampler.peak_rss_bytes is None
        assert sampler.peak_cpu_percent is None
        assert sampler.mean_cpu_percent is None
        assert sampler.last_rss_bytes is None
        assert sampler.last_cpu_percent is None


class TestLastReading:
    def test_last_reading_is_the_most_recent_not_the_peak(self):
        """Progress display wants 'what is it doing right now', which a peak
        cannot answer once the job has passed its high point."""
        sampler = ResourceSampler(pid=1)
        sampler.observe(FakeProcess(rss=900, cpu=10.0))
        sampler.observe(FakeProcess(rss=100, cpu=5.0))
        assert sampler.last_rss_bytes == 100
        assert sampler.last_cpu_percent == 5.0
        # The peak is unaffected by tracking the last reading alongside it.
        assert sampler.peak_rss_bytes == 900
        assert sampler.peak_cpu_percent == 10.0


class TestChildCpuAcrossPolls:
    """`psutil.Process.children()` hands back brand-new Process objects on
    every call. `cpu_percent(interval=None)` reports the delta since the
    *previous call on that exact instance* -- 0.0 unconditionally on an
    instance's first call. If `observe()` read whichever fresh object
    `children()` just returned, every child's cpu_percent would read 0.0 on
    every poll, forever. These tests use a different `FakeProcess` instance
    per poll (mirroring what psutil actually does) to prove `observe()`
    persists and reuses one Process per child pid instead of trusting object
    identity.
    """

    def test_reuses_the_persisted_process_for_a_child_seen_again_by_pid(self):
        sampler = ResourceSampler(pid=1)

        child_poll_1 = FakeProcess(rss=100, cpu=5.0, pid=42)
        root_poll_1 = FakeProcess(rss=50, cpu=1.0, children=[child_poll_1])
        sampler.observe(root_poll_1)

        # A second poll: a DIFFERENT FakeProcess object represents the same
        # OS process (same pid) -- exactly what psutil.children() does. Its
        # own cpu value (40.0) must never be read: proving that is the whole
        # point of this test, since the old code would have read exactly it.
        child_poll_2 = FakeProcess(rss=110, cpu=40.0, pid=42)
        root_poll_2 = FakeProcess(rss=55, cpu=1.0, children=[child_poll_2])
        sampler.observe(root_poll_2)

        # The old bug: cpu_percent would be read from child_poll_2 (a fresh
        # object, first call in this fake's terms -- and in real psutil,
        # unconditionally 0.0 on that instance's first call). The fix: the
        # sampler must look up its own persisted Process for pid 42 --
        # seeded from child_poll_1 -- and call cpu_percent on THAT object,
        # never on child_poll_2 at all.
        assert child_poll_2.cpu_percent_calls == 0
        assert child_poll_1.cpu_percent_calls == 2
        # Second-poll total is root(1.0) + persisted-child(5.0, from
        # child_poll_1) == 6.0, NOT root(1.0) + child_poll_2's 40.0 -- if the
        # sampler had read child_poll_2 instead, this would be 41.0.
        assert sampler.peak_cpu_percent == pytest.approx(6.0)

    def test_a_newly_seen_child_is_tracked_for_next_time(self):
        """A child appearing for the first time still reads 0.0 on this poll
        (unavoidable -- it just started) but must be persisted so the NEXT
        poll reuses it rather than reading fresh-every-time forever."""
        sampler = ResourceSampler(pid=1)

        child = FakeProcess(rss=100, cpu=0.0, pid=7)
        sampler.observe(FakeProcess(rss=10, cpu=0.0, children=[child]))
        assert 7 in sampler._child_procs
        assert sampler._child_procs[7] is child

    def test_a_child_pid_no_longer_present_is_dropped_from_tracking(self):
        """A child that exits between polls must not linger as a stale
        entry that raises on every future observe() forever."""
        sampler = ResourceSampler(pid=1)

        child = FakeProcess(rss=100, cpu=5.0, pid=99)
        sampler.observe(FakeProcess(rss=10, cpu=0.0, children=[child]))
        assert 99 in sampler._child_procs

        # Next poll: pid 99 no longer appears under the root at all (exited
        # and reaped, not merely erroring on read).
        sampler.observe(FakeProcess(rss=10, cpu=0.0, children=[]))
        assert 99 not in sampler._child_procs

    def test_a_persisted_child_that_starts_raising_is_evicted(self):
        """If the persisted Process itself starts raising (the child exited
        but psutil still returned it in children() this one last time), it
        must be dropped rather than kept around to raise again next time."""
        sampler = ResourceSampler(pid=1)

        child_poll_1 = FakeProcess(rss=100, cpu=5.0, pid=13)
        sampler.observe(FakeProcess(rss=10, cpu=0.0, children=[child_poll_1]))
        assert 13 in sampler._child_procs

        # children() still lists pid 13 this poll, but reading it now fails.
        child_poll_1._gone = True
        # unused; read comes from the persisted one
        child_poll_2 = FakeProcess(rss=1, cpu=1.0, pid=13)
        sampler.observe(FakeProcess(rss=10, cpu=0.0, children=[child_poll_2]))
        assert 13 not in sampler._child_procs
