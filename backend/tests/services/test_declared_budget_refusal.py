"""Launch-time refusal of a job that could never be claimed (#478).

The bug: a job whose declared mem_mb exceeds the admission budget can never
satisfy claim.lua's `mem <= mem_free`, has no starvation escape, and no
timeout -- so it waits forever. These tests pin the refusal that replaces
that wait.
"""

import pytest

from app.errors import ValidationError
from app.pipelines import resource_estimator
from app.services import pipeline_service, resource_limit_service


def test_over_budget_declaration_raises():
    """R1: a declaration above the budget is refused, not queued."""
    with pytest.raises(ValidationError) as excinfo:
        pipeline_service.refuse_if_over_budget(
            declared_mb=16384, budget_mb=5600, resource_override=False
        )
    assert "16,384" in str(excinfo.value)
    assert "5,600" in str(excinfo.value)


def test_override_skips_the_refusal():
    """R3: 'Launch anyway' proceeds; claim.lua admits it under sole occupancy."""
    pipeline_service.refuse_if_over_budget(
        declared_mb=16384, budget_mb=5600, resource_override=True
    )


def test_within_budget_declaration_is_unaffected():
    """R6: the regression guard -- normal jobs see no new refusal."""
    pipeline_service.refuse_if_over_budget(
        declared_mb=2048, budget_mb=5600, resource_override=False
    )


def test_equal_to_budget_is_allowed():
    """claim.lua admits on `mem <= mem_free`, so equality must fit here too."""
    pipeline_service.refuse_if_over_budget(
        declared_mb=5600, budget_mb=5600, resource_override=False
    )


def test_unknown_assembly_declaration_exceeds_a_modest_budget():
    """R6a: the case with no estimate at all.

    An assembly nothing can estimate declares UNKNOWN_ASSEMBLY_MEM_MB and is
    banded by nothing -- both launch sites guard their banding on
    `estimate is not None`. This asserts the value is genuinely over a modest
    budget, which is what makes placing the check outside that guard load-bearing.
    """
    budget = resource_limit_service.admission_budget_mb(
        stored_mb=8000, machine_mb=32000
    )
    assert resource_estimator.exceeds_declared_budget(
        declared_mb=pipeline_service.UNKNOWN_ASSEMBLY_MEM_MB, budget_mb=budget
    )
    with pytest.raises(ValidationError):
        pipeline_service.refuse_if_over_budget(
            declared_mb=pipeline_service.UNKNOWN_ASSEMBLY_MEM_MB,
            budget_mb=budget,
            resource_override=False,
        )
