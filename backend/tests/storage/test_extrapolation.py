"""Whether the question being asked sits inside the fit's evidence.

r_squared says how well a fit describes its own samples. It says nothing about
an input far outside them -- which, given every existing row came from test
data, is exactly what the first real run will be.
"""

from app.services.timing_service import _observed_range


class TestInsideTheRange:
    def test_an_input_within_the_samples_is_not_flagged(self):
        samples = [(1000, 10), (5000, 50), (10_000, 100)]
        result = _observed_range(samples, 5000)
        assert result["extrapolating"] is False
        assert result["factor_beyond"] is None

    def test_the_exact_maximum_is_still_inside(self):
        samples = [(1000, 10), (10_000, 100)]
        assert _observed_range(samples, 10_000)["extrapolating"] is False


class TestBeyondTheRange:
    def test_a_larger_input_is_flagged_with_how_far(self):
        """'8x larger than anything measured' is a materially different claim
        from an estimate inside the range."""
        samples = [(1000, 10), (10_000, 100)]
        result = _observed_range(samples, 80_000)
        assert result["extrapolating"] is True
        assert result["factor_beyond"] == 8.0

    def test_reports_the_observed_bounds(self):
        samples = [(1000, 10), (10_000, 100)]
        result = _observed_range(samples, 80_000)
        assert result["min_observed_bytes"] == 1000
        assert result["max_observed_bytes"] == 10_000

    def test_a_smaller_input_is_not_flagged(self):
        """Interpolating below the smallest sample is a mild claim; the fit's
        intercept covers it. Only extrapolating upward risks a large error."""
        samples = [(10_000, 100), (20_000, 200)]
        assert _observed_range(samples, 500)["extrapolating"] is False


class TestDegenerateInput:
    def test_no_samples_reports_no_opinion(self):
        result = _observed_range([], 1000)
        assert result["extrapolating"] is False
        assert result["max_observed_bytes"] is None

    def test_all_zero_sizes_do_not_divide_by_zero(self):
        result = _observed_range([(0, 10), (0, 20)], 5000)
        assert result["extrapolating"] is True
        assert result["factor_beyond"] is None
