from app.pipelines.aligners import Aligner
from app.services import replan_service
from app.services.replan_service import JOB_TYPE_ALIGN_READS


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


def _align_params(**overrides) -> dict:
    base = {
        "aligner": Aligner.MINIMAP2.value,
        "threads": 8,
        "sort_memory_mb": 1024,
        "reference_bases": 3_000_000_000,
        "building_index": False,
    }
    base.update(overrides)
    return base


def test_index_dominated_job_is_infeasible_not_a_descent_to_one_thread():
    """A reference whose index alone busts the budget cannot be re-planned.

    This asserts the refusal, which is the direction that fails if the
    feasibility test breaks. A descent that "succeeds" here would be proposing
    a single-threaded run that still does not fit.
    """
    result = replan_service.replan(
        job_type=JOB_TYPE_ALIGN_READS,
        # minimap2's index is 1.5 bytes/base, so 3 Gbase is ~4.3 GB of index
        # alone -- well over a 2 GB budget no matter what threads do. (The
        # floor configuration, 4 threads at the 64 MB minimum sort buffer,
        # estimates 7,108 MB.)
        params=_align_params(),
        budget_mb=2_000,
        cpu_budget=16.0,
    )

    assert isinstance(result, replan_service.Infeasible)
    assert "2,000 MB" in result.reason


def test_thread_floor_prevents_an_absurd_single_threaded_proposal():
    """Only fitting below half the baseline means infeasible, not 1 thread."""
    result = replan_service.replan(
        job_type=JOB_TYPE_ALIGN_READS,
        params=_align_params(threads=16, sort_memory_mb=1024),
        # Tight enough that even 8 threads at the minimum sort buffer is over.
        budget_mb=1_400,
        cpu_budget=16.0,
    )

    assert isinstance(result, replan_service.Infeasible)
