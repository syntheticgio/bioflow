"""A configuration that fits, or a concrete reason none does.

When a job is refused for exceeding the memory budget, the useful response is
a configuration that would fit. This module computes one.

**It changes computational settings only -- never data.** Threads, sort buffer
sizes, and settings of that kind. Not the reads, not the reference, not the
files involved. A proposal can make a run slower; it cannot make it answer a
different question, which is what makes applying one automatically safe.

Each job type registers its own `propose()` function rather than declaring
knobs to a generic search. Tools that do the same job tune differently --
minimap2, winnowmap and STAR do not share one story, and STAR's index build is
a different shape of problem entirely. A declarative schema would force them
into one strategy and start growing escape hatches immediately.

The registry is the *intentionally partial* kind described in CLAUDE.md: most
job types have nothing to tune, so an absent entry is an honest answer
(`NoKnobs`) rather than a gap to be filled. A job type belongs here when it has
at least one computational setting that measurably changes predicted memory and
can be lowered without changing what the job computes.
"""

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Redefined rather than imported from pipeline_service: that module will import
# this one once the refusal card is wired up, and the constant is a string
# literal that has never changed.
JOB_TYPE_ALIGN_READS = "align_reads"
JOB_TYPE_ASSEMBLE = "assemble_reads"


@dataclass(frozen=True)
class Change:
    """One knob moved, and where it moved from and to."""

    name: str
    before: int
    after: int


@dataclass(frozen=True)
class Proposal:
    """A configuration verified to fit the budget.

    `note` carries the capacity-clamp sentence when the clamp fired, empty
    otherwise. It is reported separately from `changes` because a clamp and a
    descent mean different things: "your machine has 16 cores" is a fact about
    the hardware, while "14 GB -> 7 GB" is a fact about the budget. Collapsing
    them into one arrow loses the explanation a user most needs.
    """

    params: dict
    estimate_mb: int
    changes: list[Change] = field(default_factory=list)
    note: str = ""


@dataclass(frozen=True)
class Infeasible:
    """No configuration of the available knobs fits, and why.

    `reason` diagnoses without suggesting. "The index alone needs 18 GB of your
    16 GB budget, and that term is fixed by the reference size" states the
    binding constraint and lets the user draw their own conclusion. It must not
    say "use a smaller reference" -- that is a biology decision and out of
    scope for a feature that changes computational settings only.
    """

    reason: str


@dataclass(frozen=True)
class NoKnobs:
    """This job type has no computational settings worth turning.

    Deliberately distinct from `Infeasible`. "I tried and nothing fits" and
    "there is nothing here I know how to tune" call for different next steps,
    and collapsing both into a single None would lose exactly the distinction
    the user needs.
    """


ReplanResult = Proposal | Infeasible | NoKnobs

# job type -> propose(). Populated in later tasks.
_PROPOSERS: dict = {}

# job type -> a function mapping proposed params to an estimate in MB.
# Kept parallel to _PROPOSERS rather than bundled into one entry so the
# verification path cannot be satisfied by whatever the proposer felt like
# reporting: the wrapper calls this, never `Proposal.estimate_mb`.
_VERIFIERS: dict = {}


def replan(
    *,
    job_type: str,
    params: dict,
    budget_mb: int,
    cpu_budget: float,
) -> ReplanResult:
    """Propose a fitting configuration for this job, or say why there is none.

    Every `Proposal` is verified here against the same estimator that produced
    the refusal, before it is returned. A per-type function that miscomputes
    degrades to `Infeasible` -- never to a button that is offered and then
    refused.

    Verification failure is a bug in the per-type function, not a user-facing
    condition, so it logs rather than raises: raising at enqueue time would
    turn a refusal card into a 500.
    """
    proposer = _PROPOSERS.get(job_type)
    if proposer is None:
        return NoKnobs()

    result = proposer(params=params, budget_mb=budget_mb, cpu_budget=cpu_budget)
    if not isinstance(result, Proposal):
        return result

    verifier = _VERIFIERS.get(job_type)
    if verifier is None:
        logger.error(
            "replan: %s has a proposer but no verifier; refusing to offer "
            "an unverified proposal",
            job_type,
        )
        return Infeasible(
            "A smaller configuration was found but could not be confirmed to "
            "fit. Nothing has been changed."
        )

    confirmed_mb = verifier(result.params)
    if confirmed_mb > budget_mb:
        logger.error(
            "replan: %s proposed %s claiming %d MB, but verification says "
            "%d MB against a %d MB budget",
            job_type,
            result.params,
            result.estimate_mb,
            confirmed_mb,
            budget_mb,
        )
        return Infeasible(
            "A smaller configuration was found but could not be confirmed to "
            "fit. Nothing has been changed."
        )

    return result


def _clamp_threads(*, threads: int, cpu_budget: float) -> tuple[int, str]:
    """Stage one: reduce a thread count the machine cannot deliver.

    Unconditional and budget-independent. A hundred threads on a sixteen core
    machine is incoherent whether or not memory happens to fit -- this is not a
    memory negotiation, it is a request that was never coherent.

    Returns the clamped count and a sentence explaining it, or the original
    count and an empty string when no clamp was needed. The sentence matters
    more than it looks: the user launching a hundred-thread job may simply not
    know what their machine can do, and this is the line that teaches them.
    """
    capacity = max(1, int(cpu_budget))
    if threads <= capacity:
        return threads, ""

    return capacity, (
        f"{threads} threads is more than this machine can run; "
        f"it has {capacity} cores."
    )


def _align_estimate(params: dict) -> int:
    """Re-estimate an alignment from a params dict.

    Used both by the descent and by the verification wrapper, so a proposal is
    always confirmed against the identical arithmetic that produced it.
    """
    from app.pipelines import resource_estimator
    from app.pipelines.aligners import Aligner

    return resource_estimator.estimate_mb(
        aligner=Aligner(params["aligner"]),
        reference_bases=params["reference_bases"],
        threads=params["threads"],
        sort_memory_mb=params["sort_memory_mb"],
        building_index=params["building_index"],
    )


def _propose_align(*, params: dict, budget_mb: int, cpu_budget: float) -> ReplanResult:
    """Alignment: clamp threads, then descend sort buffer, then threads."""
    from app.pipelines import resource_estimator
    from app.pipelines.align_params import MIN_SORT_MEMORY_MB
    from app.pipelines.aligners import Aligner

    original_threads = params["threads"]
    original_sort_mb = params["sort_memory_mb"]

    baseline_threads, note = _clamp_threads(
        threads=original_threads, cpu_budget=cpu_budget
    )

    # The feasibility test. If the cheapest configuration the floors permit is
    # still over budget, no descent can succeed -- the fixed index term
    # dominates, and threads cannot reduce it. One estimate call answers this.
    thread_floor = max(1, baseline_threads // 2)
    floor_params = {
        **params,
        "threads": thread_floor,
        "sort_memory_mb": MIN_SORT_MEMORY_MB,
    }
    if _align_estimate(floor_params) > budget_mb:
        return Infeasible(
            resource_estimator.explain(
                aligner=Aligner(params["aligner"]),
                reference_bases=params["reference_bases"],
                threads=thread_floor,
                sort_memory_mb=MIN_SORT_MEMORY_MB,
                building_index=params["building_index"],
                mem_budget_mb=budget_mb,
            )
        )

    # Stage two. Descend the cheaper knob first: halving the sort buffer costs
    # some I/O, halving threads costs wall-clock roughly proportionally.
    sort_mb = original_sort_mb
    threads = baseline_threads

    while True:
        current = {**params, "threads": threads, "sort_memory_mb": sort_mb}
        if _align_estimate(current) <= budget_mb:
            break

        if sort_mb > MIN_SORT_MEMORY_MB:
            sort_mb = max(MIN_SORT_MEMORY_MB, sort_mb // 2)
            continue

        # Sort buffer is at its floor; threads are the only knob left. Halve
        # rather than decrement: the terms are linear in threads, so a linear
        # scan buys nothing but iterations.
        if threads > thread_floor:
            threads = max(thread_floor, threads // 2)
            continue

        # Unreachable: the feasibility test above already proved the floor
        # configuration fits. Kept because a future knob added to the descent
        # without updating that test would otherwise loop forever.
        return Infeasible(
            f"No configuration within the available settings fits the "
            f"{budget_mb:,} MB budget."
        )

    changes = []
    if threads != original_threads:
        changes.append(Change(name="threads", before=original_threads, after=threads))
    if sort_mb != original_sort_mb:
        changes.append(
            Change(name="sort_memory_mb", before=original_sort_mb, after=sort_mb)
        )

    if not changes:
        # Nothing needed to move -- the configuration already fits. Callers
        # reach this only if they asked for a re-plan of a job that was not
        # actually refused.
        return NoKnobs()

    final = {**params, "threads": threads, "sort_memory_mb": sort_mb}
    return Proposal(
        params=final,
        estimate_mb=_align_estimate(final),
        changes=changes,
        note=note,
    )


_PROPOSERS[JOB_TYPE_ALIGN_READS] = _propose_align
_VERIFIERS[JOB_TYPE_ALIGN_READS] = _align_estimate
