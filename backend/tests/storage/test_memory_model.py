"""Peak-memory prediction from measured runs.

Pure arithmetic, tested without Mongo -- matching test_timing_model.py, which
tests the duration fit the same way and for the same reason.
"""

import pytest

from app.services.timing_service import MIN_SAMPLES, _fit_memory


class TestInsufficientData:
    def test_no_samples_gives_no_model(self):
        assert _fit_memory([]) is None

    @pytest.mark.parametrize("n", range(1, MIN_SAMPLES))
    def test_below_threshold_gives_no_model(self, n):
        """Same silence-before-confidence rule as the duration model: a
        confidently wrong memory number invites an OOM."""
        assert _fit_memory([(1000 * i, 100 * i) for i in range(1, n + 1)]) is None


class TestFit:
    def test_recovers_a_known_slope(self):
        """1 byte of RSS per byte of input, plus 100 MB fixed."""
        base = 100 * 1024 * 1024
        samples = [(1_000_000 * i, base + 1_000_000 * i) for i in range(1, 21)]
        model = _fit_memory(samples)
        assert model["slope"] == pytest.approx(1.0, rel=1e-6)
        assert model["intercept"] == pytest.approx(base, rel=1e-6)

    def test_constant_memory_yields_a_flat_model(self):
        """Many tools have a footprint set by the reference, not the reads --
        a flat model is the right answer, not a failure."""
        samples = [(1_000_000 * i, 2 * 1024**3) for i in range(1, 21)]
        model = _fit_memory(samples)
        assert model["flat"] is True
        assert model["intercept"] == pytest.approx(2 * 1024**3, rel=1e-6)


class TestExclusions:
    def test_samples_without_a_peak_are_dropped_before_fitting(self):
        """Runs under the sampling floor carry None, not zero. Treating them
        as zero would drag every prediction toward nothing."""
        from app.services.timing_service import _memory_samples_from

        class FakeRecord:
            def __init__(self, input_bytes, peak):
                self.input_bytes = input_bytes

                class R:
                    peak_rss_bytes = peak

                self.resources = R()

        records = [FakeRecord(1000, None), FakeRecord(2000, 5000), FakeRecord(3000, 0)]
        assert _memory_samples_from(records) == [(2000, 5000)]
