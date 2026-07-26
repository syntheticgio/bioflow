"""Priority scoring and anti-starvation promotion."""

from datetime import UTC, datetime, timedelta

from app.models import JobClass
from app.queue.priority import (
    BASE_SCORES,
    MAX_AGE_COMPONENT,
    PROMOTION_AFTER_SECONDS,
    class_of_score,
    compute_score,
    promoted_score,
    promotion_cutoff_score,
)

# Deliberately close to EPOCH_BASE_MS: the age term is only meaningful in the
# window after it, and using a far-future date would silently exercise the
# clamp instead of the ordering logic.
NOW = datetime(2026, 1, 5, 12, 0, 0, tzinfo=UTC)


class TestOrdering:
    def test_classes_rank_in_the_intended_order(self):
        """A user-initiated job must outrank background work enqueued earlier."""
        scores = {c: compute_score(c, NOW) for c in JobClass}
        assert (
            scores[JobClass.USER_INTERACTIVE]
            < scores[JobClass.USER_BACKGROUND]
            < scores[JobClass.MAINTENANCE]
            < scores[JobClass.BULK]
        )

    def test_fifo_within_a_class(self):
        earlier = compute_score(JobClass.MAINTENANCE, NOW)
        later = compute_score(JobClass.MAINTENANCE, NOW + timedelta(seconds=30))
        assert earlier < later

    def test_class_dominates_age_within_reasonable_waits(self):
        """A day-old maintenance job still yields to a fresh user job.

        Only the promotion sweep changes this, which is the point: ordering is
        predictable, and escalation is explicit rather than emergent.
        """
        old_maintenance = compute_score(JobClass.MAINTENANCE, NOW - timedelta(days=1))
        fresh_user = compute_score(JobClass.USER_INTERACTIVE, NOW)
        assert fresh_user < old_maintenance

    def test_age_can_never_cross_into_another_class_band(self):
        """The regression this clamp exists for: with an unbounded age term,
        seconds-since-epoch grows past the class gap within days and inverts
        the entire ordering."""
        far_future = datetime(2099, 1, 1, tzinfo=UTC)
        for job_class in JobClass:
            score = compute_score(job_class, far_future)
            assert class_of_score(score) is job_class
            assert score - BASE_SCORES[job_class] <= MAX_AGE_COMPONENT

    def test_ancient_low_priority_still_loses_to_fresh_user_work(self):
        ancient_bulk = compute_score(JobClass.BULK, datetime(2026, 1, 1, tzinfo=UTC))
        fresh_user = compute_score(JobClass.USER_INTERACTIVE, datetime(2099, 1, 1, tzinfo=UTC))
        assert fresh_user < ancient_bulk


class TestClassRecovery:
    def test_recovers_class_from_score(self):
        for job_class in JobClass:
            score = compute_score(job_class, NOW)
            assert class_of_score(score) is job_class


class TestPromotion:
    def test_promotes_one_tier(self):
        assert promoted_score(
            compute_score(JobClass.MAINTENANCE, NOW), JobClass.MAINTENANCE
        ) is not None

    def test_bulk_promotes_to_maintenance(self):
        score = compute_score(JobClass.BULK, NOW)
        promoted = promoted_score(score, JobClass.BULK)
        assert class_of_score(promoted) is JobClass.MAINTENANCE

    def test_user_interactive_does_not_promote(self):
        """There is no tier above it."""
        assert promoted_score(compute_score(JobClass.USER_INTERACTIVE, NOW),
                              JobClass.USER_INTERACTIVE) is None

    def test_promotion_preserves_relative_age(self):
        """Two promoted jobs keep their order rather than collapsing together."""
        older = compute_score(JobClass.MAINTENANCE, NOW - timedelta(minutes=30))
        newer = compute_score(JobClass.MAINTENANCE, NOW - timedelta(minutes=20))
        assert promoted_score(older, JobClass.MAINTENANCE) < promoted_score(
            newer, JobClass.MAINTENANCE
        )

    def test_cutoff_selects_only_jobs_past_the_threshold(self):
        threshold = PROMOTION_AFTER_SECONDS[JobClass.MAINTENANCE]
        cutoff = promotion_cutoff_score(JobClass.MAINTENANCE, NOW)

        waited_too_long = compute_score(
            JobClass.MAINTENANCE, NOW - timedelta(seconds=threshold + 60)
        )
        just_enqueued = compute_score(JobClass.MAINTENANCE, NOW)

        assert waited_too_long <= cutoff
        assert just_enqueued > cutoff

    def test_cutoff_stays_inside_the_class_band(self):
        """The sweep queries by score range, so the cutoff must not spill into
        a neighbouring class and promote the wrong jobs."""
        cutoff = promotion_cutoff_score(JobClass.MAINTENANCE, NOW)
        assert BASE_SCORES[JobClass.MAINTENANCE] <= cutoff
        assert cutoff < BASE_SCORES[JobClass.BULK]

    def test_starvation_is_bounded_for_every_promotable_class(self):
        for job_class, threshold in PROMOTION_AFTER_SECONDS.items():
            waited = compute_score(
                job_class, NOW - timedelta(seconds=threshold + 1)
            )
            assert waited <= promotion_cutoff_score(job_class, NOW), job_class
