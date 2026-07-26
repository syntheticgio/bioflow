"""Priority scoring and anti-starvation promotion.

Lowest score dispatches first. A score is `class base + seconds since epoch`,
which yields FIFO ordering inside a class for free and keeps the whole ordering
in a single sorted set.

Aging is handled by a periodic promotion sweep rather than continuous
recomputation. Continuously re-scoring every queued job would be O(n log n) on
every tick; promotion touches only the jobs that actually crossed a threshold,
and the resulting behavior is far easier to reason about.
"""

from datetime import UTC, datetime, timedelta

from app.models import JobClass

# Gaps are wide enough that a lower class never merely edges past a higher one
# through rounding, but finite so promotion remains meaningful.
BASE_SCORES: dict[JobClass, int] = {
    JobClass.USER_INTERACTIVE: 0,
    JobClass.USER_BACKGROUND: 1_000_000,
    JobClass.MAINTENANCE: 5_000_000,
    JobClass.BULK: 9_000_000,
}

# Reference point for the age term. It must be recent enough that the age
# component never grows into the next class's band: with a fixed historical
# epoch, seconds-since-epoch reaches millions within days and silently inverts
# the class ordering entirely.
EPOCH_BASE_MS = 1_767_225_600_000  # 2026-01-01T00:00:00Z

# Ceiling on the age term, well below the 1,000,000 gap between adjacent
# classes. Ordering within a class stays strictly FIFO up to this many seconds
# of queue wait (~11.5 days); beyond that, jobs tie and the promotion sweep --
# not the raw score -- is what escalates them.
MAX_AGE_COMPONENT = 999_000

# How long a job may wait in its class before being promoted one tier.
# Bulk is deliberately absent: whole-library sweeps should yield indefinitely.
PROMOTION_AFTER_SECONDS: dict[JobClass, int] = {
    JobClass.USER_BACKGROUND: 300,  # 5 min behind user-interactive work
    JobClass.MAINTENANCE: 900,  # 15 min
    JobClass.BULK: 3600,  # 1 hour
}

PROMOTION_TARGET: dict[JobClass, JobClass] = {
    JobClass.USER_BACKGROUND: JobClass.USER_INTERACTIVE,
    JobClass.MAINTENANCE: JobClass.USER_BACKGROUND,
    JobClass.BULK: JobClass.MAINTENANCE,
}


def age_component(at: datetime) -> float:
    """Sub-class ordering term: seconds since the epoch base, clamped.

    Clamping is what guarantees a job's age can never carry it into another
    class's score band. Escalation across classes is the promotion sweep's job,
    and keeping it there is what makes dispatch order predictable.
    """
    seconds = (int(at.timestamp() * 1000) - EPOCH_BASE_MS) / 1000.0
    return max(0.0, min(seconds, MAX_AGE_COMPONENT))


def compute_score(job_class: JobClass, enqueued_at: datetime | None = None) -> float:
    """Dispatch score for a job. Lower wins."""
    enqueued_at = enqueued_at or datetime.now(UTC)
    return BASE_SCORES[job_class] + age_component(enqueued_at)


def class_of_score(score: float) -> JobClass:
    """Recover the effective class from a score, including after promotion."""
    for job_class in (JobClass.BULK, JobClass.MAINTENANCE, JobClass.USER_BACKGROUND):
        if score >= BASE_SCORES[job_class]:
            return job_class
    return JobClass.USER_INTERACTIVE


def promoted_score(current_score: float, job_class: JobClass) -> float | None:
    """Score for a job moving up one tier, preserving its relative age.

    Returns None when the class does not promote.
    """
    target = PROMOTION_TARGET.get(job_class)
    if target is None:
        return None
    age_component = current_score - BASE_SCORES[job_class]
    return BASE_SCORES[target] + age_component


def promotion_cutoff_score(job_class: JobClass, now: datetime | None = None) -> float:
    """Scores at or below this have waited long enough to be promoted."""
    now = now or datetime.now(UTC)
    threshold = PROMOTION_AFTER_SECONDS.get(job_class)
    if threshold is None:
        return float("-inf")
    cutoff_at = now - timedelta(seconds=threshold)
    return BASE_SCORES[job_class] + age_component(cutoff_at)
