"""Reading the user's resource budget, and resolving it against the host.

Split from the model so the arithmetic is pure and testable without a worker
or a host probe -- the same reason `worker.compute_free_resources` is pure.
"""

from app.config import settings
from app.models.base import utcnow
from app.models.resource_limits import ResourceLimits


def resolve_mem_budget_mb(
    *, stored_mb: int | None, machine_mb: int, hard_mem_mb: int | None = None
) -> int:
    """The memory ceiling admission should compute headroom against.

    A stored limit only ever *lowers* the budget. Typing 64 GB on a 16 GB
    machine cannot conjure headroom, and letting it try would over-admit
    exactly as badly as having no limit at all -- the number is a budget to
    stay under, not a claim about the hardware.

    Zero and negatives are treated as "no opinion" rather than as a real
    ceiling of nothing. A literal zero budget would admit no job ever and
    stall the queue with no error anywhere, which is the silent-failure shape
    this codebase already goes out of its way to avoid.

    `hard_mem_mb` is the kernel-enforced cgroup ceiling when one is set. It
    binds unconditionally: a soft budget above it would admit jobs the kernel
    then kills, which is the worst available outcome. Below it is normal and
    expected -- that is admission doing its job and the wall never being hit.
    """
    budget = machine_mb
    if stored_mb is not None and stored_mb > 0:
        budget = min(stored_mb, machine_mb)
    if hard_mem_mb is not None and hard_mem_mb > 0:
        budget = min(budget, hard_mem_mb)
    return budget


# Headroom so a job that slightly overshoots its declared demand does not push
# the machine into swap. Extracted from worker._resource_budgets, which applied
# it as a bare literal: the launch-time refusal must use the same figure, and
# two copies of a constant that must agree is how they come to disagree.
MEM_HEADROOM_FRACTION = 0.7


def admission_budget_mb(
    *, stored_mb: int | None, machine_mb: int, hard_mem_mb: int | None = None
) -> int:
    """The stable ceiling admission plans against, before live headroom.

    This is the number that decides whether a job can *ever* be claimed.
    `worker._resource_budgets` clamps it further by a live `available_mb`
    reading, which moves with whatever else is running; the launch-time check
    deliberately does not, because a job under this ceiling is claimable once
    the machine is quiet, and refusing it would be a false refusal. See the
    spec's "The budget is not the configured limit".
    """
    resolved = resolve_mem_budget_mb(
        stored_mb=stored_mb, machine_mb=machine_mb, hard_mem_mb=hard_mem_mb
    )
    return max(int(resolved * MEM_HEADROOM_FRACTION), 0)


def hard_mem_mb() -> int | None:
    """The kernel-enforced ceiling, or None when hard limits are off.

    Reads the value the launcher passed in rather than a cgroup file: `api`
    is deliberately uncapped, so its own cgroup reports `max`.
    """
    configured = settings.bioflow_hard_mem_mb
    if configured is None or configured <= 0:
        return None
    return configured


async def load() -> ResourceLimits:
    """The stored limits, created on first read."""
    return await ResourceLimits.load()


async def save(
    *,
    max_mem_mb: int | None,
    max_cpu: float | None,
    max_threads: int | None,
) -> ResourceLimits:
    """Replace the stored limits.

    Every field is written on every save, including None. The UI's "No limit"
    must be able to *clear* a previously-set ceiling, so an absent value here
    means "no limit" rather than "leave unchanged" -- the opposite of
    ProviderUpdate's three-way api_key semantics, and deliberately simpler
    because there is no secret to preserve.
    """
    limits = await ResourceLimits.load()
    limits.max_mem_mb = max_mem_mb
    limits.max_cpu = max_cpu
    limits.max_threads = max_threads
    limits.updated_at = utcnow()
    await limits.save()
    return limits
