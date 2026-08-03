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

    def __init__(self, rss, cpu, children=None, gone=False):
        self._rss = rss
        self._cpu = cpu
        self._children = children or []
        self._gone = gone

    def memory_info(self):
        if self._gone:
            raise ProcessLookupError("process is gone")
        return type("MemInfo", (), {"rss": self._rss})()

    def cpu_percent(self, interval=None):
        if self._gone:
            raise ProcessLookupError("process is gone")
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
