"""eta_seconds: choosing between extrapolation and the prior-runs model.

Two estimators, and the choice between them is the whole design point.
`elapsed / pct` self-corrects as a run proceeds but is worthless near the
start -- the first percent of a job is usually its least representative
stretch (process startup, index loading), so at pct=0.01 the extrapolation
multiplies elapsed time by a hundred. `timing_service.estimate()`'s model is
available at t=0 but blind to how *this* run is actually going. Prefer
extrapolation above the floor, fall back to the model, null when neither
applies.

A pure function taking numbers rather than documents, so every branch is
testable without a database.
"""

import pytest

from app.services.timing_service import ETA_PCT_FLOOR, eta_seconds


class TestExtrapolation:
    def test_extrapolates_from_elapsed_and_pct_above_the_floor(self):
        # 50% done after 100s implies another 100s remain.
        result = eta_seconds(pct=0.5, elapsed_s=100.0, model_ms=None)
        assert result == pytest.approx(100.0, rel=0.01)

    def test_prefers_extrapolation_over_the_model_when_both_are_available(self):
        """The run's own progress is a better signal than a prior-runs model
        the moment there is enough of it to trust."""
        result = eta_seconds(pct=0.5, elapsed_s=100.0, model_ms=999_000)
        assert result == pytest.approx(100.0, rel=0.01)

    def test_a_finished_job_has_zero_remaining(self):
        result = eta_seconds(pct=1.0, elapsed_s=100.0, model_ms=None)
        assert result == pytest.approx(0.0, abs=0.01)


class TestFloor:
    def test_pct_below_the_floor_does_not_extrapolate(self):
        """The regression this floor exists for: at pct=0.01, elapsed/pct
        multiplies elapsed time by 100 -- exactly the startup noise the floor
        is meant to reject. Falls back to the model instead."""
        result = eta_seconds(pct=0.01, elapsed_s=5.0, model_ms=120_000)
        assert result == pytest.approx(115.0, rel=0.01)

    def test_pct_exactly_at_the_floor_does_extrapolate(self):
        result = eta_seconds(pct=ETA_PCT_FLOOR, elapsed_s=10.0, model_ms=None)
        expected = 10.0 / ETA_PCT_FLOOR - 10.0
        assert result == pytest.approx(expected, rel=0.01)


class TestFallbackToTheModel:
    def test_no_pct_falls_back_to_the_model(self):
        """eta_seconds means *remaining* time, so the model's predicted total
        duration still has elapsed time subtracted from it -- a job 5s into a
        predicted 60s run has 55s left, not 60."""
        result = eta_seconds(pct=None, elapsed_s=5.0, model_ms=60_000)
        assert result == pytest.approx(55.0, rel=0.01)

    def test_a_run_already_past_the_models_predicted_total_has_zero_remaining(self):
        """The model can be wrong; a negative ETA is never shown."""
        result = eta_seconds(pct=None, elapsed_s=90.0, model_ms=60_000)
        assert result == pytest.approx(0.0, abs=0.01)

    def test_no_pct_and_no_model_is_null(self):
        """Every phase-only job with no history yet -- the honest answer is
        that nothing here can say."""
        result = eta_seconds(pct=None, elapsed_s=5.0, model_ms=None)
        assert result is None

    def test_pct_below_floor_and_no_model_is_null(self):
        result = eta_seconds(pct=0.01, elapsed_s=5.0, model_ms=None)
        assert result is None
