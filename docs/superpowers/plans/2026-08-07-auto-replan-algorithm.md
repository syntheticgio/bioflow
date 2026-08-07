# Auto Re-plan Algorithm Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When BioFlow refuses a job for exceeding the memory budget, compute a configuration of computational settings that is predicted to fit — or explain concretely why none exists.

**Architecture:** One new module, `app/services/replan_service.py`. It holds a registry mapping job type to a per-type `propose()` function, a three-way result type (`Proposal` / `Infeasible` / `NoKnobs`), and a `replan()` entry point that wraps every per-type proposal in a verification step re-running the same estimator that produced the refusal. Everything is pure — budgets are passed in, nothing probes the machine or touches the database — so the whole engine is testable without a live host.

**Tech Stack:** Python 3.12, dataclasses, `StrEnum`. Tests with pytest. Existing modules consumed: `app.pipelines.resource_estimator` (`estimate_mb`, `estimate_assembly_mb`, `explain`), `app.pipelines.align_params` (`MIN_SORT_MEMORY_MB`), `app.pipelines.assembly_params` (`MIN_THREADS`).

**Spec:** `docs/superpowers/specs/2026-08-07-auto-replan-algorithm-design.md`

---

## Background the engineer needs

**What "re-plan" may change.** Computational settings only: thread counts, sort buffer sizes. Never the data — not the reads, not the reference, not the files. Splitting a job into pieces is a future capability and is not built here. This constraint is what makes the proposal safe to apply automatically: it can make a run slower, never make it answer a different question.

**Two BLOCK sites exist today**, both raising `ValidationError` as a dead end:
- `app/services/pipeline_service.py:1486` — alignment
- `app/services/pipeline_service.py:3279` — assembly

This plan does **not** modify either. It builds the engine those sites (and issue #70's refusal card) will later call. Wiring is out of scope; the engine ships standalone with its own tests.

**The memory arithmetic**, from `resource_estimator.estimate_mb()`:

```
total = fixed_overhead_mb + index_mb + (threads * bytes_per_thread_mb) + (threads * sort_memory_mb)
```

`index_mb` is `reference_bases * index_bytes_per_ref_base / 1MB`, multiplied by `index_build_multiplier` when building. **The index term does not depend on threads** — it is the floor. If it alone exceeds the budget, no thread count helps.

Assembly, from `estimate_assembly_mb()`:

```
total = fixed_overhead_mb + graph_mb + (threads * mb_per_thread)
```

`graph_mb` is `genome_bases * bytes_per_genome_base / 1MB`, and plays the same fixed-floor role. This function returns `None` when genome size is unknown — a real answer meaning "no opinion", not a failure.

**Two stages, and they mean different things:**

1. **Capacity clamp** — if `threads > cpu_budget`, clamp to `int(cpu_budget)`. Unconditional and budget-independent: 100 threads on a 16-core machine is incoherent whether or not memory fits. The clamped value is the baseline for stage two.
2. **Memory descent** — from the *post-clamp* baseline, descend knobs until the estimate fits.

**The thread floor is `max(1, baseline // 2)` where `baseline` is post-clamp.** This is the plan's most important correction to issue #71, which says "half the original thread count". Half of an incoherent original is still incoherent: 100 threads floors at 50, still does not fit, reports infeasible — denying a proposal in exactly the case that most needs one.

**Knob order: `sort_memory_mb` descends before `threads`.** Halving the sort buffer costs some I/O; halving threads costs wall-clock roughly proportionally. Cheaper knob first.

**No duration field anywhere.** `timing_service.estimate()` is thread-blind until issue #8, so asking it about a thread change returns no change — a confident lie. The engine does not call it and has no duration field, so there is nothing for a caller to read by accident.

---

## File Structure

**Create:**
- `backend/app/services/replan_service.py` — the whole engine: result types, registry, verification wrapper, and both per-type `propose()` functions. Single file because the pieces are small and change together; the per-type functions are ~30 lines each and splitting them across modules would obscure that they are alternative implementations of one interface.
- `backend/tests/services/test_replan_service.py` — all tests for the above.

**Modify:** nothing. This task builds a module that nothing yet calls.

---

## Task 1: Result types and the empty registry

**Files:**
- Create: `backend/app/services/replan_service.py`
- Create: `backend/tests/services/test_replan_service.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/test_replan_service.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_replan_service.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.replan_service'`

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/services/replan_service.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/services/test_replan_service.py -q`

Expected: PASS, 2 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/replan_service.py backend/tests/services/test_replan_service.py
git commit -m "feat: replan result types and empty proposer registry (#71)"
```

---

## Task 2: The verification wrapper

This is the engine's one guarantee: a proposal is offered only if re-running the estimator against the proposed parameters confirms it fits. Without it, "the button never appears without a fitting configuration" is a convention each per-type author must remember rather than a structural property.

The wrapper needs a way to re-estimate arbitrary proposed params, which differs per job type. So the registry stores a pair: the proposer and a verifier that maps params to an estimate.

**Files:**
- Modify: `backend/app/services/replan_service.py`
- Test: `backend/tests/services/test_replan_service.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/services/test_replan_service.py`:

```python
def test_verification_downgrades_a_lying_proposal(monkeypatch):
    """A propose() that returns an over-budget proposal must not be offered.

    This asserts the guarantee itself. Without this test the wrapper is
    untested code that only runs when something else is already broken.
    """

    def lying_proposer(*, params, budget_mb, cpu_budget):
        return replan_service.Proposal(
            params={"threads": 4},
            estimate_mb=1,  # claims 1 MB
            changes=[replan_service.Change(name="threads", before=8, after=4)],
        )

    def honest_estimator(params):
        return 99_000  # actually 99 GB

    monkeypatch.setitem(
        replan_service._PROPOSERS, "fake_job", lying_proposer
    )
    monkeypatch.setitem(
        replan_service._VERIFIERS, "fake_job", honest_estimator
    )

    result = replan_service.replan(
        job_type="fake_job",
        params={"threads": 8},
        budget_mb=16_000,
        cpu_budget=16.0,
    )

    assert isinstance(result, replan_service.Infeasible)
    assert "could not be confirmed" in result.reason


def test_verification_passes_an_honest_proposal(monkeypatch):
    def honest_proposer(*, params, budget_mb, cpu_budget):
        return replan_service.Proposal(params={"threads": 4}, estimate_mb=7_000)

    monkeypatch.setitem(
        replan_service._PROPOSERS, "fake_job", honest_proposer
    )
    monkeypatch.setitem(
        replan_service._VERIFIERS, "fake_job", lambda params: 7_000
    )

    result = replan_service.replan(
        job_type="fake_job",
        params={"threads": 8},
        budget_mb=16_000,
        cpu_budget=16.0,
    )

    assert isinstance(result, replan_service.Proposal)
    assert result.estimate_mb == 7_000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_replan_service.py -q`

Expected: FAIL with `AttributeError: module 'app.services.replan_service' has no attribute '_VERIFIERS'`

- [ ] **Step 3: Write minimal implementation**

In `backend/app/services/replan_service.py`, add the logging import at the top of the import block:

```python
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)
```

Replace the `_PROPOSERS` declaration and the `replan()` function with:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/services/test_replan_service.py -q`

Expected: PASS, 4 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/replan_service.py backend/tests/services/test_replan_service.py
git commit -m "feat: verify every replan proposal before offering it (#71)"
```

---

## Task 3: The capacity clamp

Stage one, shared by both job types: a thread count above the machine's core count is incoherent regardless of budget.

**Files:**
- Modify: `backend/app/services/replan_service.py`
- Test: `backend/tests/services/test_replan_service.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/services/test_replan_service.py`:

```python
def test_clamp_reduces_threads_above_core_count():
    clamped, note = replan_service._clamp_threads(threads=100, cpu_budget=16.0)
    assert clamped == 16
    assert "16" in note
    assert note != ""


def test_clamp_leaves_a_sane_thread_count_alone():
    clamped, note = replan_service._clamp_threads(threads=8, cpu_budget=16.0)
    assert clamped == 8
    assert note == ""


def test_clamp_floors_at_one_thread():
    """A fractional or sub-1 cpu_budget must never clamp to zero threads."""
    clamped, note = replan_service._clamp_threads(threads=4, cpu_budget=0.5)
    assert clamped == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_replan_service.py -q`

Expected: FAIL with `AttributeError: module 'app.services.replan_service' has no attribute '_clamp_threads'`

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/services/replan_service.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/services/test_replan_service.py -q`

Expected: PASS, 7 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/replan_service.py backend/tests/services/test_replan_service.py
git commit -m "feat: capacity clamp for thread counts above core count (#71)"
```

---

## Task 4: Alignment proposer — feasibility test and index-dominated refusal

The feasibility test first, because it is the cheap answer and the one issue #71 calls out: if the estimate at the floor configuration still exceeds budget, no descent can succeed.

The floor configuration is `threads = max(1, post_clamp_baseline // 2)` and `sort_memory_mb = MIN_SORT_MEMORY_MB`.

**Files:**
- Modify: `backend/app/services/replan_service.py`
- Test: `backend/tests/services/test_replan_service.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/services/test_replan_service.py`. Note the import at the top of the file must grow — add `from app.pipelines.aligners import Aligner` alongside the existing import:

```python
from app.pipelines.aligners import Aligner
from app.services.pipeline_service import JOB_TYPE_ALIGN_READS


def _align_params(**overrides) -> dict:
    base = {
        "aligner": Aligner.MINIMAP2.value,
        "threads": 8,
        "sort_memory_mb": 1024,
        "reference_bases": 3_000_000_000,
        "building_index": False,
    }
    base.update(overrides)
    return base


def test_index_dominated_job_is_infeasible_not_a_descent_to_one_thread():
    """A reference whose index alone busts the budget cannot be re-planned.

    This asserts the refusal, which is the direction that fails if the
    feasibility test breaks. A descent that "succeeds" here would be proposing
    a single-threaded run that still does not fit.
    """
    result = replan_service.replan(
        job_type=JOB_TYPE_ALIGN_READS,
        # minimap2's index is 1.5 bytes/base, so 3 Gbase is ~4.3 GB of index
        # alone -- well over a 2 GB budget no matter what threads do. (The
        # floor configuration, 4 threads at the 64 MB minimum sort buffer,
        # estimates 7,108 MB.)
        params=_align_params(),
        budget_mb=2_000,
        cpu_budget=16.0,
    )

    assert isinstance(result, replan_service.Infeasible)
    assert "2,000 MB" in result.reason


def test_thread_floor_prevents_an_absurd_single_threaded_proposal():
    """Only fitting below half the baseline means infeasible, not 1 thread."""
    result = replan_service.replan(
        job_type=JOB_TYPE_ALIGN_READS,
        params=_align_params(threads=16, sort_memory_mb=1024),
        # Tight enough that even 8 threads at the minimum sort buffer is over.
        budget_mb=1_400,
        cpu_budget=16.0,
    )

    assert isinstance(result, replan_service.Infeasible)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_replan_service.py -q`

Expected: FAIL — both new tests return `NoKnobs` because nothing is registered for `align_reads` yet, so `isinstance(result, Infeasible)` is False.

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/services/replan_service.py`:

```python
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

    # Descent lands in the next task.
    return NoKnobs()


_PROPOSERS[JOB_TYPE_ALIGN_READS] = _propose_align
_VERIFIERS[JOB_TYPE_ALIGN_READS] = _align_estimate
```

Add the job type constant near the top of the file, after the `logger` line. It is redefined rather than imported from `pipeline_service` to avoid a circular import — `pipeline_service` will import this module once #70 wires it up:

```python
# Redefined rather than imported from pipeline_service: that module will import
# this one once the refusal card is wired up, and the constant is a string
# literal that has never changed.
JOB_TYPE_ALIGN_READS = "align_reads"
JOB_TYPE_ASSEMBLE = "assemble_reads"
```

Remove the `from app.services.pipeline_service import JOB_TYPE_ALIGN_READS` line from the test file and import it from `replan_service` instead:

```python
from app.services.replan_service import JOB_TYPE_ALIGN_READS
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/services/test_replan_service.py -q`

Expected: PASS, 9 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/replan_service.py backend/tests/services/test_replan_service.py
git commit -m "feat: alignment feasibility test refuses index-dominated jobs (#71)"
```

---

## Task 5: Alignment descent — sort buffer first, then threads

**Files:**
- Modify: `backend/app/services/replan_service.py`
- Test: `backend/tests/services/test_replan_service.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/services/test_replan_service.py`:

```python
def test_sort_buffer_descends_before_threads():
    """A job that fits by halving the sort buffer keeps all its threads.

    Halving the sort buffer costs some I/O; halving threads costs wall-clock
    roughly proportionally. The cheaper knob has to move first.
    """
    # 8 threads x 1024 MB sort = 8192 MB of sort buffer alone (of a 12,944 MB
    # total). Budget set to exactly the halved-sort estimate, 8,848 MB: it
    # fits the aligner side plus a reduced sort buffer, but not the full one.
    params = _align_params(reference_bases=100_000_000, threads=8, sort_memory_mb=1024)
    full = replan_service._align_estimate(params)
    halved = replan_service._align_estimate({**params, "sort_memory_mb": 512})

    result = replan_service.replan(
        job_type=JOB_TYPE_ALIGN_READS,
        params=params,
        budget_mb=halved,  # exactly fits the halved-sort configuration
        cpu_budget=16.0,
    )

    assert isinstance(result, replan_service.Proposal)
    assert result.params["threads"] == 8, "threads must not move when sort alone fits"
    assert result.params["sort_memory_mb"] < 1024
    assert result.estimate_mb <= halved
    assert full > halved  # guards the fixture's own premise


def test_hundred_thread_request_is_clamped_to_core_count():
    """The case issue #71 as written would have refused.

    A floor of "half the original" would put this at 50 threads, which does not
    fit, reporting infeasible. Halving the post-clamp baseline gives a floor of
    8 instead, and the descent finds a fit before reaching it.

    Verified arithmetic: clamped to 16 threads the estimate is 25,232 MB,
    still over the 16,000 MB budget, so the sort buffer descends 1024 -> 512
    -> 256, at which point it fits. Threads therefore land at exactly the
    clamp, and the sort buffer moves too.
    """
    result = replan_service.replan(
        job_type=JOB_TYPE_ALIGN_READS,
        params=_align_params(reference_bases=100_000_000, threads=100),
        budget_mb=16_000,
        cpu_budget=16.0,
    )

    assert isinstance(result, replan_service.Proposal)
    assert result.params["threads"] == 16, "clamped to the core count"
    assert result.params["sort_memory_mb"] == 256
    assert result.estimate_mb <= 16_000
    assert "16 cores" in result.note
    names = {c.name for c in result.changes}
    assert names == {"threads", "sort_memory_mb"}


def test_proposal_records_before_and_after_for_each_moved_knob():
    result = replan_service.replan(
        job_type=JOB_TYPE_ALIGN_READS,
        params=_align_params(reference_bases=100_000_000, threads=100),
        budget_mb=16_000,
        cpu_budget=16.0,
    )

    assert isinstance(result, replan_service.Proposal)
    threads_change = next(c for c in result.changes if c.name == "threads")
    assert threads_change.before == 100
    assert threads_change.after == result.params["threads"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_replan_service.py -q`

Expected: FAIL — all three return `NoKnobs` (the descent placeholder), so the `isinstance(..., Proposal)` assertions fail.

- [ ] **Step 3: Write minimal implementation**

In `backend/app/services/replan_service.py`, replace the `# Descent lands in the next task.` / `return NoKnobs()` lines at the end of `_propose_align` with:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/services/test_replan_service.py -q`

Expected: PASS, 12 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/replan_service.py backend/tests/services/test_replan_service.py
git commit -m "feat: alignment descent moves sort buffer before threads (#71)"
```

---

## Task 6: Assembly proposer

Same two stages, one knob. `estimate_assembly_mb()` returning `None` means `NoKnobs` — no opinion is not a refusal.

**Files:**
- Modify: `backend/app/services/replan_service.py`
- Test: `backend/tests/services/test_replan_service.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/services/test_replan_service.py`, and add `from app.pipelines.assemblers import Assembler` and `from app.services.replan_service import JOB_TYPE_ASSEMBLE` to the imports at the top:

```python
def _assembly_params(**overrides) -> dict:
    base = {
        "assembler": Assembler.FLYE.value,
        "threads": 16,
        "genome_bases": 100_000_000,
    }
    base.update(overrides)
    return base


def test_assembly_without_a_genome_size_reports_no_knobs():
    """No opinion is not a refusal.

    estimate_assembly_mb returns None when genome size is unknown, which is the
    normal case for de novo assembly rather than a misconfiguration. Reporting
    Infeasible here would refuse a job we simply cannot predict.
    """
    result = replan_service.replan(
        job_type=JOB_TYPE_ASSEMBLE,
        params=_assembly_params(genome_bases=None),
        budget_mb=16_000,
        cpu_budget=16.0,
    )

    assert isinstance(result, replan_service.NoKnobs)


def test_graph_dominated_assembly_is_infeasible():
    """Flye is 40 bytes/base, so a 3 Gbase genome needs ~114 GB of graph.

    Verified: at the 8-thread floor the estimate is 117,513 MB, far over the
    16,000 MB budget, and no thread count touches the graph term.
    """
    result = replan_service.replan(
        job_type=JOB_TYPE_ASSEMBLE,
        params=_assembly_params(genome_bases=3_000_000_000),
        budget_mb=16_000,
        cpu_budget=16.0,
    )

    assert isinstance(result, replan_service.Infeasible)
    assert "16,000 MB" in result.reason


def test_assembly_descends_threads_to_fit():
    params = _assembly_params(genome_bases=100_000_000, threads=16)
    at_eight = replan_service._assembly_estimate({**params, "threads": 8})

    result = replan_service.replan(
        job_type=JOB_TYPE_ASSEMBLE,
        params=params,
        budget_mb=at_eight,
        cpu_budget=16.0,
    )

    assert isinstance(result, replan_service.Proposal)
    assert result.params["threads"] == 8
    assert result.estimate_mb <= at_eight
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_replan_service.py -q`

Expected: FAIL with `AttributeError: module 'app.services.replan_service' has no attribute '_assembly_estimate'`

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/services/replan_service.py`:

```python
def _assembly_estimate(params: dict) -> int | None:
    """Re-estimate an assembly. None when genome size is unknown."""
    from app.pipelines import resource_estimator
    from app.pipelines.assemblers import Assembler

    return resource_estimator.estimate_assembly_mb(
        assembler=Assembler(params["assembler"]),
        genome_bases=params["genome_bases"],
        threads=params["threads"],
    )


def _propose_assembly(
    *, params: dict, budget_mb: int, cpu_budget: float
) -> ReplanResult:
    """Assembly: one knob, and a graph term that plays the index's role.

    The repeat graph is fixed by the genome size and cannot be reduced by any
    setting here, so it is the floor the feasibility test checks.
    """
    from app.pipelines.assembly_params import MIN_THREADS

    original_threads = params["threads"]

    baseline_threads, note = _clamp_threads(
        threads=original_threads, cpu_budget=cpu_budget
    )

    thread_floor = max(MIN_THREADS, baseline_threads // 2)
    floor_estimate = _assembly_estimate({**params, "threads": thread_floor})

    # None means the genome size is unknown. Not knowing is not a refusal --
    # see estimate_assembly_mb's docstring, which makes the same point.
    if floor_estimate is None:
        return NoKnobs()

    if floor_estimate > budget_mb:
        return Infeasible(
            f"This assembly needs about {floor_estimate:,} MB even at "
            f"{thread_floor} threads, more than the {budget_mb:,} MB budget. "
            f"Most of that is the repeat graph, which is fixed by the genome "
            f"size rather than by any setting here."
        )

    threads = baseline_threads
    while threads > thread_floor:
        current = _assembly_estimate({**params, "threads": threads})
        if current is not None and current <= budget_mb:
            break
        threads = max(thread_floor, threads // 2)

    changes = []
    if threads != original_threads:
        changes.append(Change(name="threads", before=original_threads, after=threads))

    if not changes:
        return NoKnobs()

    final = {**params, "threads": threads}
    estimate = _assembly_estimate(final)
    # Cannot be None: the floor estimate above already answered for these
    # inputs, and threads do not affect whether genome_bases is known.
    assert estimate is not None
    return Proposal(
        params=final,
        estimate_mb=estimate,
        changes=changes,
        note=note,
    )


def _assembly_verifier(params: dict) -> int:
    """Verification wants a number, and by this point there always is one."""
    estimate = _assembly_estimate(params)
    # A proposal is only produced when the estimate is known, so a None here
    # means the params were mutated between proposal and verification.
    return estimate if estimate is not None else 2**31


_PROPOSERS[JOB_TYPE_ASSEMBLE] = _propose_assembly
_VERIFIERS[JOB_TYPE_ASSEMBLE] = _assembly_verifier
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/services/test_replan_service.py -q`

Expected: PASS, 15 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/replan_service.py backend/tests/services/test_replan_service.py
git commit -m "feat: assembly proposer descends threads against the graph floor (#71)"
```

---

## Task 7: Registry reachability test

The spec's substitute for enum exhaustiveness. This registry is intentionally partial — most job types have nothing to tune — so what it needs is proof that every entry present can actually fire, catching a registration that can never produce a proposal.

**Files:**
- Test: `backend/tests/services/test_replan_service.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/services/test_replan_service.py`:

```python
# Inputs that should produce a Proposal for each registered job type. A new
# registry entry without a row here fails the test below, which is the point:
# a proposer that can never fire is invisible otherwise.
_REACHABILITY_CASES = {
    JOB_TYPE_ALIGN_READS: (
        {
            "aligner": Aligner.MINIMAP2.value,
            "threads": 100,
            "sort_memory_mb": 1024,
            "reference_bases": 100_000_000,
            "building_index": False,
        },
        16_000,
    ),
    JOB_TYPE_ASSEMBLE: (
        {
            "assembler": Assembler.FLYE.value,
            "threads": 100,
            "genome_bases": 100_000_000,
        },
        16_000,
    ),
}


def test_every_registered_proposer_is_reachable():
    assert set(_REACHABILITY_CASES) == set(replan_service._PROPOSERS), (
        "every registered proposer needs a reachability case"
    )

    for job_type, (params, budget_mb) in _REACHABILITY_CASES.items():
        result = replan_service.replan(
            job_type=job_type,
            params=params,
            budget_mb=budget_mb,
            cpu_budget=16.0,
        )
        assert isinstance(result, replan_service.Proposal), (
            f"{job_type} produced {type(result).__name__}, not a Proposal"
        )


def test_every_proposer_has_a_verifier():
    """A proposer without a verifier can never offer anything."""
    assert set(replan_service._PROPOSERS) == set(replan_service._VERIFIERS)
```

- [ ] **Step 2: Run test to verify it fails**

Both tests should PASS immediately if Tasks 4–6 are correct. To confirm the reachability test actually detects a broken registration, temporarily add a dead entry:

```python
replan_service._PROPOSERS["fake"] = lambda **kw: replan_service.NoKnobs()
```

Run: `./backend/run-worktree-tests.sh tests/services/test_replan_service.py::test_every_registered_proposer_is_reachable -q`

Expected: FAIL with "every registered proposer needs a reachability case". **Remove the temporary line before continuing.**

- [ ] **Step 3: Run the full file**

Run: `./backend/run-worktree-tests.sh tests/services/test_replan_service.py -q`

Expected: PASS, 17 passed

- [ ] **Step 4: Run the whole suite**

Run: `./backend/run-worktree-tests.sh tests/ -q`

Expected: all tests pass. Read the count — a passing exit code from a run that collected nothing is not green. Compare against the pre-change baseline; this plan adds 17 tests and modifies no existing behaviour, so the only delta should be +17.

- [ ] **Step 5: Commit**

```bash
git add backend/tests/services/test_replan_service.py
git commit -m "test: every registered replan proposer is reachable (#71)"
```

---

## Task 8: Check against a real reference object

CLAUDE.md records that green unit tests on hand-built objects have shipped wrong behaviour in this repo before — the suggestion rules passed a full suite while counting `protein.faa` as an alignable reference, because the fixtures already looked the way the rules expected. Every test above feeds `_propose_align` a dict this plan wrote.

This task closes that gap as far as it can be closed before #70's card exists to show a proposal.

**Files:** none modified. This is a verification step.

- [ ] **Step 1: Find a real reference object and re-plan against it**

From the **worktree root**, with the worktree stack up (`./ops/worktree-up.sh`):

```bash
docker compose -p biopipe-worktree exec api python -c "
import asyncio
from app.db.client import connect_to_mongo
from app.services import object_service, replan_service
from app.pipelines.aligners import Aligner

async def main():
    await connect_to_mongo()
    refs = await object_service.list_objects(limit=200)
    for obj in refs:
        if not obj.size or obj.size < 1_000_000:
            continue
        params = {
            'aligner': Aligner.MINIMAP2.value,
            'threads': 100,
            'sort_memory_mb': 1024,
            'reference_bases': obj.size,
            'building_index': False,
        }
        result = replan_service.replan(
            job_type=replan_service.JOB_TYPE_ALIGN_READS,
            params=params, budget_mb=16000, cpu_budget=16.0,
        )
        print(obj.name, obj.size, type(result).__name__, getattr(result, 'note', ''), getattr(result, 'changes', getattr(result, 'reason', '')))
        break

asyncio.run(main())
"
```

If `list_objects` has a different signature in this checkout, find it with `grep -n "def list_objects" backend/app/services/object_service.py` and adjust — the point is to reach a real stored object, not to run this exact line.

- [ ] **Step 2: Confirm the output is sane**

Expected: a `Proposal` whose `note` names the core count, whose `changes` show `threads: 100 -> <=16`, and whose estimate is under 16000. For a large reference (a multi-gigabase genome), expect `Infeasible` with a reason naming the aligner's own footprint.

**What would falsify this:** a proposal claiming to fit while its estimate exceeds the budget, a clamp that leaves threads above the core count, or an `Infeasible` reason that reads as a template rather than naming real numbers.

- [ ] **Step 3: Record the result**

Note in the commit message what was observed — the object size used and which branch fired. If anything looked wrong, stop and fix it rather than proceeding: this is the check the unit tests cannot make.

- [ ] **Step 4: Commit**

```bash
git commit --allow-empty -m "chore: verify replan against a real reference object (#71)"
```

---

## Task 9: Merge and close out

- [ ] **Step 1: Confirm the suite is green**

Run: `./backend/run-worktree-tests.sh tests/ -q`

Read the count. Green means the number, not the exit code.

- [ ] **Step 2: Merge to main**

```bash
git checkout main && git pull && git merge claude/issue-71-brainstorm-f6518e
```

If `main` has moved, re-run the suite after merging rather than assuming the earlier green still holds:

```bash
docker compose exec api python -m pytest tests/ -q
```

(That command is correct from the **main checkout root** only.)

- [ ] **Step 3: Push**

```bash
git push origin main
```

- [ ] **Step 4: Update the issue**

```bash
gh issue edit 71 --repo syntheticgio/bioflow --remove-label "status: implementation plan" --add-label "status:ready"
```

Then comment on [#71](https://github.com/syntheticgio/bioflow/issues/71) with what shipped, what the implementation did differently from this plan, and the note that the engine is built but **not yet wired to either BLOCK site** — that is #70's work. Do not close the issue: the user-visible feature is not reachable until the card renders it.

- [ ] **Step 5: Note the follow-up**

Comment on [#70](https://github.com/syntheticgio/bioflow/issues/70) that `replan_service.replan()` now exists and is what the Auto re-plan button should call, with the three-way result to render.

---

## Self-review notes

**Spec coverage.** Every section of the design maps to a task: result types and the registry (1), the verification guarantee (2), the capacity clamp (3), the feasibility test and infeasible-with-reason (4), the two-knob descent and knob order (5), assembly including the `None` case (6), the reachability substitute for exhaustiveness (7), the real-object check the spec names as a planned gap-closer (8).

**Deliberately absent, per the spec:** no duration field anywhere; no modification to either BLOCK site; no WARN-band call site; no job splitting.

**One thing this plan decided that the spec left open.** The spec says the engine re-runs "the same estimator the refusal used" but does not say where that estimator comes from. This plan uses a `_VERIFIERS` dict parallel to `_PROPOSERS` rather than bundling both into one registry entry, so the wrapper always calls the verifier and never trusts `Proposal.estimate_mb`. Bundling them would let a per-type author satisfy verification with the same wrong number twice.

**The arithmetic in every test was checked against the real coefficients**
(minimap2 at 1.5 bytes/base, `fixed_overhead_mb=512`, `bytes_per_thread_mb=512`;
Flye at 40.0 bytes/base, `fixed_overhead_mb=2048`, `mb_per_thread=128`) by
simulating the descent before this plan was written, not by reasoning about it.
Two errors were caught that way: a comment citing bwa-mem2's 2.0 bytes/base as
minimap2's, and a 100-thread assertion that assumed only threads would move
when the sort buffer moves too. Expected outcomes per case:

| Case | Budget | Result |
| --- | --- | --- |
| 3 Gbase ref, 8t/1024 | 2,000 | Infeasible (floor is 7,108) |
| 100 Mbase ref, 16t/1024 | 1,400 | Infeasible (floor is 5,264) |
| 100 Mbase ref, 8t/1024 | 8,848 | Proposal: 8t, sort 512 |
| 100 Mbase ref, 100t/1024 | 16,000 | Proposal: 16t, sort 256 |
| Flye 3 Gbase, 16t | 16,000 | Infeasible (floor is 117,513) |
| Flye 100 Mbase, 16t | 6,887 | Proposal: 8t |

**Type consistency.** `Change(name, before, after)`, `Proposal(params, estimate_mb, changes, note)`, `Infeasible(reason)`, `NoKnobs()` are used identically in every task. `_align_estimate` / `_assembly_estimate` / `_clamp_threads` keep their signatures from definition through use. The proposer signature is `(*, params, budget_mb, cpu_budget)` everywhere including the test doubles.
