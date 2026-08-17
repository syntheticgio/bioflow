"""Duration prediction from run history.

The fitting logic is tested directly rather than through Mongo: the arithmetic
is what can be wrong, and keeping it pure makes the edge cases (identical
sizes, negative slopes, outliers) easy to state.
"""

import pytest

from app.models.timing import JobRunTiming
from app.services.timing_service import (
    MIN_SAMPLES,
    _duration_samples_from,
    _fit,
    _fit_segmented,
    _r_squared,
)


class TestInsufficientData:
    def test_no_samples_gives_no_model(self):
        """Silence before confidence: a made-up progress bar is worse than
        none at all."""
        assert _fit([]) is None

    @pytest.mark.parametrize("n", range(1, MIN_SAMPLES))
    def test_below_threshold_gives_no_model(self, n):
        assert _fit([(1000 * i, 100 * i) for i in range(1, n + 1)]) is None

    def test_exactly_the_threshold_produces_a_model(self):
        samples = [(1_000_000 * i, 1000 * i) for i in range(1, MIN_SAMPLES + 1)]
        assert _fit(samples) is not None


class TestLinearFit:
    def test_recovers_a_known_slope(self):
        """1 ms per 1000 bytes, no intercept."""
        samples = [(1000 * i, 1 * i) for i in range(1, 21)]
        model = _fit(samples)
        assert model["slope"] == pytest.approx(0.001, rel=1e-6)
        assert model["intercept"] == pytest.approx(0, abs=1e-6)

    def test_recovers_slope_and_intercept(self):
        """500 ms fixed startup cost plus 1 ms per 1000 bytes."""
        samples = [(1000 * i, 500 + i) for i in range(1, 21)]
        model = _fit(samples)
        assert model["slope"] == pytest.approx(0.001, rel=1e-6)
        assert model["intercept"] == pytest.approx(500, rel=1e-6)

    def test_prediction_matches_the_fit(self):
        samples = [(1000 * i, 500 + i) for i in range(1, 21)]
        m = _fit(samples)
        predicted = m["intercept"] + m["slope"] * 50_000
        assert predicted == pytest.approx(550, rel=0.01)

    def test_perfect_fit_has_r_squared_one(self):
        samples = [(1000 * i, 2 * i) for i in range(1, 11)]
        assert _r_squared(samples, _fit(samples)) == pytest.approx(1.0, abs=1e-9)

    def test_noisy_data_lowers_r_squared(self):
        """A poor fit must be visible so the UI can hedge the estimate.

        Enough points that outlier rejection cannot drop the set below the
        MIN_SAMPLES floor -- otherwise this tests the floor, not the fit.
        """
        import random

        rng = random.Random(42)
        # Size and duration are uncorrelated, so no line explains the variance.
        samples = [(1000 * i, rng.randint(400, 1600)) for i in range(1, 41)]
        model = _fit(samples)
        assert model is not None
        assert _r_squared(samples, model) < 0.5


class TestRobustness:
    def test_identical_sizes_fall_back_to_the_mean(self):
        """No spread in x means no derivable slope, but the mean is still a
        good estimate for that one size."""
        samples = [(1_000_000, 1000 + i) for i in range(10)]
        model = _fit(samples)
        assert model["flat"] is True
        assert model["slope"] == 0.0
        assert model["intercept"] == pytest.approx(1004.5, rel=0.01)

    def test_negative_slope_falls_back_to_the_mean(self):
        """Bigger files finishing faster is noise, not signal -- predicting a
        negative duration would be worse than predicting the average."""
        samples = [(1000 * i, 1000 - 50 * i) for i in range(1, 11)]
        model = _fit(samples)
        assert model["slope"] == 0.0
        assert model["intercept"] > 0

    def test_outliers_are_dropped(self):
        """One page-cache hit or one contended run should not move the fit."""
        clean = [(1000 * i, 100 * i) for i in range(1, 11)]
        with_outlier = clean + [(5000, 500_000)]  # 1000x slower
        assert _fit(with_outlier)["slope"] == pytest.approx(
            _fit(clean)["slope"], rel=0.2
        )

    def test_intercept_is_never_negative(self):
        """A negative intercept would predict negative durations for small
        inputs."""
        samples = [(1000 * i, 10 * i) for i in range(5, 25)]
        assert _fit(samples)["intercept"] >= 0

    def test_zero_byte_inputs_do_not_crash(self):
        assert _fit([(0, 100)] * MIN_SAMPLES) is not None


def _timing(*, threads, input_bytes, duration_ms):
    """An unsaved JobRunTiming for feeding straight into `_fit_segmented`
    without touching Mongo -- the grouping logic is pure over the list."""
    return JobRunTiming(
        job_type="align_reads",
        input_bytes=input_bytes,
        duration_ms=duration_ms,
        threads=threads,
    )


class TestSegmentedFit:
    def test_segments_with_enough_samples_get_their_own_fit(self, beanie_models):
        """Two thread counts, each with MIN_SAMPLES rows and a different
        slope: each must be fit independently, not pooled."""
        records = [
            _timing(threads=4, input_bytes=1000 * i, duration_ms=1 * i)
            for i in range(1, MIN_SAMPLES + 1)
        ] + [
            _timing(threads=8, input_bytes=1000 * i, duration_ms=3 * i)
            for i in range(1, MIN_SAMPLES + 1)
        ]
        segments = _fit_segmented(records, _duration_samples_from)
        assert segments[4]["slope"] == pytest.approx(0.001, rel=1e-6)
        assert segments[8]["slope"] == pytest.approx(0.003, rel=1e-6)

    def test_fallback_key_is_none_and_pools_every_record(self, beanie_models):
        """The None entry is the bytes-only fit over ALL records, matching
        today's un-segmented behavior exactly."""
        records = [
            _timing(threads=4, input_bytes=1000 * i, duration_ms=1 * i)
            for i in range(1, MIN_SAMPLES + 1)
        ]
        pooled = _fit(_duration_samples_from(records))
        segments = _fit_segmented(records, _duration_samples_from)
        assert segments[None]["slope"] == pytest.approx(pooled["slope"])
        assert segments[None]["n"] == pooled["n"]

    def test_sparse_thread_count_gets_no_segment_entry(self, beanie_models):
        """Fewer than MIN_SAMPLES rows at a thread count: no key for it at
        all, so callers fall through to the None fallback."""
        records = [
            _timing(threads=4, input_bytes=1000 * i, duration_ms=1 * i)
            for i in range(1, MIN_SAMPLES + 1)
        ] + [
            _timing(threads=16, input_bytes=1000, duration_ms=1)
            for _ in range(MIN_SAMPLES - 1)
        ]
        segments = _fit_segmented(records, _duration_samples_from)
        assert 4 in segments
        assert 16 not in segments
        assert None in segments

    def test_unknown_thread_count_excluded_from_segments_included_in_fallback(
        self, beanie_models
    ):
        """threads=None records never form or join a per-thread group, but
        do count toward the pooled fallback -- unknown thread count means
        'can't be assigned a segment', not 'assign it the None segment'."""
        records = [
            _timing(threads=None, input_bytes=1000 * i, duration_ms=1 * i)
            for i in range(1, MIN_SAMPLES + 1)
        ]
        segments = _fit_segmented(records, _duration_samples_from)
        assert list(segments.keys()) == [None]
        assert segments[None]["n"] == MIN_SAMPLES
