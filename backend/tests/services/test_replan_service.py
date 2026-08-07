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
