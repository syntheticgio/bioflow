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


def test_sort_buffer_descends_before_threads():
    """A job that fits by halving the sort buffer keeps all its threads.

    Halving the sort buffer costs some I/O; halving threads costs wall-clock
    roughly proportionally. The cheaper knob has to move first.
    """
    # 8 threads x 1024 MB sort = 8192 MB of sort buffer alone (of a 12,944 MB
    # total). Budget set to exactly the halved-sort estimate, 8,848 MB: it
    # fits the aligner side plus a reduced sort buffer, but not the full one.
    params = _align_params(reference_bases=100_000_000, threads=8, sort_memory_mb=1024)
    full = replan_service._align_estimate(params)
    halved = replan_service._align_estimate({**params, "sort_memory_mb": 512})

    result = replan_service.replan(
        job_type=JOB_TYPE_ALIGN_READS,
        params=params,
        budget_mb=halved,  # exactly fits the halved-sort configuration
        cpu_budget=16.0,
    )

    assert isinstance(result, replan_service.Proposal)
    assert result.params["threads"] == 8, "threads must not move when sort alone fits"
    assert result.params["sort_memory_mb"] < 1024
    assert result.estimate_mb <= halved
    assert full > halved  # guards the fixture's own premise


def test_hundred_thread_request_is_clamped_to_core_count():
    """The case issue #71 as written would have refused.

    A floor of "half the original" would put this at 50 threads, which does not
    fit, reporting infeasible. Halving the post-clamp baseline gives a floor of
    8 instead, and the descent finds a fit before reaching it.

    Verified arithmetic: clamped to 16 threads the estimate is 25,232 MB,
    still over the 16,000 MB budget, so the sort buffer descends 1024 -> 512
    -> 256, at which point it fits. Threads therefore land at exactly the
    clamp, and the sort buffer moves too.
    """
    result = replan_service.replan(
        job_type=JOB_TYPE_ALIGN_READS,
        params=_align_params(reference_bases=100_000_000, threads=100),
        budget_mb=16_000,
        cpu_budget=16.0,
    )

    assert isinstance(result, replan_service.Proposal)
    assert result.params["threads"] == 16, "clamped to the core count"
    assert result.params["sort_memory_mb"] == 256
    assert result.estimate_mb <= 16_000
    assert "16 cores" in result.note
    names = {c.name for c in result.changes}
    assert names == {"threads", "sort_memory_mb"}


def test_proposal_records_before_and_after_for_each_moved_knob():
    result = replan_service.replan(
        job_type=JOB_TYPE_ALIGN_READS,
        params=_align_params(reference_bases=100_000_000, threads=100),
        budget_mb=16_000,
        cpu_budget=16.0,
    )

    assert isinstance(result, replan_service.Proposal)
    threads_change = next(c for c in result.changes if c.name == "threads")
    assert threads_change.before == 100
    assert threads_change.after == result.params["threads"]
