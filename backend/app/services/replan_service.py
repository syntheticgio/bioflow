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

from dataclasses import dataclass, field


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


def replan(
    *,
    job_type: str,
    params: dict,
    budget_mb: int,
    cpu_budget: float,
) -> ReplanResult:
    """Propose a fitting configuration for this job, or say why there is none."""
    proposer = _PROPOSERS.get(job_type)
    if proposer is None:
        return NoKnobs()

    return proposer(params=params, budget_mb=budget_mb, cpu_budget=cpu_budget)
