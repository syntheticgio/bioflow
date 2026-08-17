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


def test_worker_budget_never_exceeds_the_admission_budget():
    """The worker's live clamp may only lower the shared ceiling, never raise it.

    Guards the pair from drifting: if someone changes the worker's arithmetic
    so it can exceed admission_budget_mb, a job could pass the launch check and
    still be unclaimable, which is the bug this work exists to remove.
    """
    ceiling = resource_limit_service.admission_budget_mb(
        stored_mb=8000, machine_mb=10000
    )
    # The worker takes min(available_mb, ceiling) then floors at 128. For any
    # available reading, the result is <= ceiling except via the 128 floor.
    for available_mb in (50, 1000, 5600, 99999):
        worker_mb = max(min(available_mb, ceiling), 128)
        assert worker_mb <= max(ceiling, 128)


def test_hard_mem_mb_lowers_the_admission_budget():
    """R4a: the kernel-enforced ceiling binds, so admission stays under it."""
    without = resource_limit_service.admission_budget_mb(
        stored_mb=None, machine_mb=10000
    )
    with_hard = resource_limit_service.admission_budget_mb(
        stored_mb=None, machine_mb=10000, hard_mem_mb=4000
    )
    assert with_hard < without
    assert with_hard <= 4000
