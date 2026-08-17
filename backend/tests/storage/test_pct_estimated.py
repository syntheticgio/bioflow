"""pct_estimated: a modelled completion fraction for jobs with no measured pct.

A pure function taking numbers rather than documents, mirroring test_eta.py's
style -- every branch testable without a database.
"""

import pytest
from app.services.timing_service import MAX_ESTIMATED_PCT, pct_estimated


class TestNoEstimateCases:
    def test_a_measured_pct_yields_nothing(self):
        """The whole point: a real pct always wins, so a parser that learns
        to measure something makes this field disappear with no caller
        change required."""
        result = pct_estimated(pct=0.5, elapsed_s=100.0, model_ms=60_000)
        assert result is None

    def test_no_model_and_no_pct_yields_nothing(self):
        """No history yet -- the bar stays indeterminate exactly as today,
        not a number nothing backs."""
        result = pct_estimated(pct=None, elapsed_s=5.0, model_ms=None)
        assert result is None

    def test_measured_pct_of_zero_still_counts_as_measured(self):
        """pct=0.0 is not the same as pct=None -- a tool that has explicitly
        reported zero progress is not the same as one reporting nothing."""
        result = pct_estimated(pct=0.0, elapsed_s=5.0, model_ms=60_000)
        assert result is None


class TestEstimating:
    def test_estimates_elapsed_over_predicted(self):
        # 30s into a predicted 60s run implies 50% modelled progress.
        result = pct_estimated(pct=None, elapsed_s=30.0, model_ms=60_000)
        assert result == pytest.approx(0.5, rel=0.01)

    def test_a_fresh_run_estimates_near_zero(self):
        result = pct_estimated(pct=None, elapsed_s=0.0, model_ms=60_000)
        assert result == pytest.approx(0.0, abs=0.01)


class TestNeverClaimsCompletion:
    def test_caps_at_the_ceiling_when_elapsed_matches_the_prediction(self):
        result = pct_estimated(pct=None, elapsed_s=60.0, model_ms=60_000)
        assert result == MAX_ESTIMATED_PCT

    def test_caps_at_the_ceiling_once_the_run_is_longer_than_expected(self):
        """The bar must not creep toward or past 100% while the job is
        genuinely still running -- a pinned 99% and a stalled job would be
        indistinguishable, the exact ambiguity nullable pct exists to avoid.
        A run at 3x its prediction gets the same capped number as a run at
        1.01x: the caller distinguishes "running long" via elapsed vs.
        model_ms itself, not via how close this fraction gets to 1.0."""
        result = pct_estimated(pct=None, elapsed_s=180.0, model_ms=60_000)
        assert result == MAX_ESTIMATED_PCT

    def test_a_zero_model_estimate_does_not_divide_by_zero(self):
        result = pct_estimated(pct=None, elapsed_s=5.0, model_ms=0)
        assert result == MAX_ESTIMATED_PCT
