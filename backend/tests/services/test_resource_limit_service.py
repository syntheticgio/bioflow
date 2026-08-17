"""Resource limit service unit tests.

Tests for the admission budget calculation and resource limiting functions.
"""

from app.services import resource_limit_service


def test_admission_budget_applies_the_headroom_fraction():
    # 10000 MB machine, no stored limit -> 70% of the machine.
    assert resource_limit_service.admission_budget_mb(
        stored_mb=None, machine_mb=10000
    ) == 7000


def test_admission_budget_applies_headroom_to_the_stored_limit():
    # A stored limit lowers the ceiling first, then headroom applies to it.
    assert resource_limit_service.admission_budget_mb(
        stored_mb=8000, machine_mb=10000
    ) == 5600


def test_admission_budget_respects_the_hard_ceiling():
    # hard_mem_mb binds unconditionally, below stored and machine alike.
    assert resource_limit_service.admission_budget_mb(
        stored_mb=8000, machine_mb=10000, hard_mem_mb=4000
    ) == 2800


def test_admission_budget_never_returns_negative():
    assert resource_limit_service.admission_budget_mb(
        stored_mb=None, machine_mb=0
    ) == 0
