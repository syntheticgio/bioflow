from app.services import replan_service


def test_unregistered_job_type_reports_no_knobs():
    result = replan_service.replan(
        job_type="summarize_object",
        params={"threads": 8},
        budget_mb=16000,
        cpu_budget=16.0,
    )
    assert isinstance(result, replan_service.NoKnobs)


def test_change_records_before_and_after():
    change = replan_service.Change(name="threads", before=16, after=8)
    assert change.name == "threads"
    assert change.before == 16
    assert change.after == 8


def test_verification_downgrades_a_lying_proposal(monkeypatch):
    """A propose() that returns an over-budget proposal must not be offered.

    This asserts the guarantee itself. Without this test the wrapper is
    untested code that only runs when something else is already broken.
    """

    def lying_proposer(*, params, budget_mb, cpu_budget):
        return replan_service.Proposal(
            params={"threads": 4},
            estimate_mb=1,  # claims 1 MB
            changes=[replan_service.Change(name="threads", before=8, after=4)],
        )

    def honest_estimator(params):
        return 99_000  # actually 99 GB

    monkeypatch.setitem(
        replan_service._PROPOSERS, "fake_job", lying_proposer
    )
    monkeypatch.setitem(
        replan_service._VERIFIERS, "fake_job", honest_estimator
    )

    result = replan_service.replan(
        job_type="fake_job",
        params={"threads": 8},
        budget_mb=16_000,
        cpu_budget=16.0,
    )

    assert isinstance(result, replan_service.Infeasible)
    assert "could not be confirmed" in result.reason


def test_verification_passes_an_honest_proposal(monkeypatch):
    def honest_proposer(*, params, budget_mb, cpu_budget):
        return replan_service.Proposal(params={"threads": 4}, estimate_mb=7_000)

    monkeypatch.setitem(
        replan_service._PROPOSERS, "fake_job", honest_proposer
    )
    monkeypatch.setitem(
        replan_service._VERIFIERS, "fake_job", lambda params: 7_000
    )

    result = replan_service.replan(
        job_type="fake_job",
        params={"threads": 8},
        budget_mb=16_000,
        cpu_budget=16.0,
    )

    assert isinstance(result, replan_service.Proposal)
    assert result.estimate_mb == 7_000


def test_clamp_reduces_threads_above_core_count():
    clamped, note = replan_service._clamp_threads(threads=100, cpu_budget=16.0)
    assert clamped == 16
    assert "16" in note
    assert note != ""


def test_clamp_leaves_a_sane_thread_count_alone():
    clamped, note = replan_service._clamp_threads(threads=8, cpu_budget=16.0)
    assert clamped == 8
    assert note == ""


def test_clamp_floors_at_one_thread():
    """A fractional or sub-1 cpu_budget must never clamp to zero threads."""
    clamped, note = replan_service._clamp_threads(threads=4, cpu_budget=0.5)
    assert clamped == 1
