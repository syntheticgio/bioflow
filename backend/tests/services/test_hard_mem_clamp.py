"""The clamp that makes a soft budget above the hard limit unrepresentable.

Without it, a 32 GB admission budget under a 16 GB kernel ceiling means every
job admission approves is then OOM-killed -- which reads as BioFlow being
broken rather than as a misconfiguration.
"""


from app.services.resource_limit_service import resolve_mem_budget_mb


def test_hard_limit_lowers_a_larger_soft_budget():
    # The case that matters: the clamp must actually bind.
    assert (
        resolve_mem_budget_mb(stored_mb=32768, machine_mb=65536, hard_mem_mb=16384)
        == 16384
    )


def test_soft_budget_below_the_hard_limit_is_untouched():
    # The normal, correct configuration -- admission keeps jobs off the wall.
    assert (
        resolve_mem_budget_mb(stored_mb=8192, machine_mb=65536, hard_mem_mb=16384)
        == 8192
    )


def test_no_hard_limit_leaves_existing_behaviour_unchanged():
    # The clamp must not become an unconditional ceiling.
    assert resolve_mem_budget_mb(stored_mb=32768, machine_mb=65536, hard_mem_mb=None) == 32768


def test_hard_limit_binds_even_with_no_soft_budget():
    # "No limit" in the web UI still cannot exceed the kernel's ceiling.
    assert (
        resolve_mem_budget_mb(stored_mb=None, machine_mb=65536, hard_mem_mb=16384)
        == 16384
    )
