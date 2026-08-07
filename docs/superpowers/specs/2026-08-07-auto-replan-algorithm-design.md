# Auto re-plan: turning computational knobs down until the job fits

Design for [#71](https://github.com/syntheticgio/bioflow/issues/71), a child of
epic [#7](https://github.com/syntheticgio/bioflow/issues/7).
Written 2026-08-07.

## What this is, and what it is not

When BioFlow refuses to start a job because its predicted memory exceeds the
budget, auto re-plan offers a configuration that is predicted to fit.

**It changes computational settings only -- never data.** Threads, sort buffer
sizes, and settings of that kind. It does not touch the number of reads, the
choice of reference, the files involved, or anything else with biological
meaning. Splitting a job into smaller pieces is a plausible future capability
and is explicitly not in this design.

That constraint is what makes the feature safe to apply automatically. A
proposal can make a run slower; it cannot make it answer a different question.

**The parent issue is written as if this were the alignment memory formula.**
It is not. The formula in #71 is one job type's arithmetic, and this design
generalizes: any job type with computational knobs registers how to turn them
down, and a shared engine turns them. Alignment and assembly are the first two
registrations rather than the subject.

## Why a per-type function rather than a declarative schema

Each job type gets a `propose()` function. The registry maps job type to that
function and the engine only orchestrates.

The tidier-looking alternative -- each job type declares
`Knob(name, floor, candidates)` and one generic descent walks them -- was
rejected. Different tools have genuinely different tuning stories, including
tools that do the same job: minimap2, winnowmap, and STAR do not share one,
and STAR's index build is a different shape of problem entirely. A declarative
schema forces them into one search strategy, and the escape hatches start
immediately (the thread floor is *derived* from the machine, not static).

This is also what CLAUDE.md's registry-audit note warns about. A registry keyed
by an enum splits three ways, and this one is the **intentionally partial**
case: most job types have nothing to tune, and forcing every enum member to be
covered would mean inventing knobs that do not exist. Forcing the middle case
into the first case's pattern is not more correct -- it is a detector that
starts guessing.

What the registry needs instead of exhaustiveness:

- **A written inclusion rule.** A job type belongs in the registry when it has
  at least one computational setting that measurably changes predicted memory
  and can be lowered without changing what the job computes.
- **A reachability test** in the other direction: every registered entry
  returns a `Proposal` for at least one input, so a registration that can never
  fire is caught.

An unregistered job type returns `NoKnobs`, which is an honest answer and
visibly distinct from a search that failed.

## The three-way result

```python
Proposal(params, estimate_mb, changes, note)   # verified to fit
Infeasible(reason)                             # nothing fits; here is why
NoKnobs()                                      # this job type has no knobs
```

Collapsing `Infeasible` and `NoKnobs` into a single `None` would lose the
distinction the user most needs. "I tried and the index alone is too big" and
"there is nothing here I know how to tune" call for different next steps.

### Infeasible carries prose, not structure

`Infeasible.reason` is a string, written by the per-type function.

`resource_estimator.explain()` already does exactly this job: it names the
dominant term and both numbers, per aligner, in prose. The infeasible reason
reuses it rather than inventing a parallel structured representation of the
same facts, which would have exactly one consumer and no one asking for it.

**The reason diagnoses; it does not suggest.** "The index alone needs 18 GB of
your 16 GB budget, and that term is fixed by the reference size" states the
binding constraint and lets the user draw their own conclusion. It must not say
"use a smaller reference" -- that is a biology decision and out of scope. This
is the informative half of the feature: when auto re-plan cannot help, it says
what is actually consuming the memory rather than failing silently.

## The engine's one guarantee

The engine owns verification, and no per-type function can skip it.

After `propose()` returns a `Proposal`, the engine re-runs **the same estimator
the refusal used** against the proposed parameters. If the result does not fit
the budget, the proposal is discarded, downgraded to `Infeasible`, and the
discrepancy is logged loudly.

That is what makes "the button never appears without a fitting configuration" a
structural property rather than a convention each per-type author has to
remember. A per-type function that miscomputes degrades to "no button" -- never
to a button that is offered and then refused.

The failure is a bug in the per-type function, not a user-facing condition, so
it does not raise: raising at enqueue time would turn a refusal card into a
500. Tests assert on it directly instead, where it is cheap to catch.

## Which budget a proposal is verified against

The whole-machine budget -- the same `governor.mem_budget_bytes()` figure that
produced the refusal -- not current free headroom.

This preserves the split the admission design already made: enqueue time asks
"can this ever fit?", claim time asks "does this fit right now?". A proposal
verified against the same number that generated the refusal can never fail the
check that produced it, which is what makes the guarantee provable.

Verifying against live headroom was rejected. It would make the same job with
the same parameters produce different proposals depending on what else happened
to be running, and it re-litigates a decision already made: contention is
queueing, not negotiation. **A proposal may still wait at claim time under
contention. That is correct and invisible.**

## The two-stage descent

Stage one and stage two are separate because they mean different things, and
collapsing them loses the explanation a user most needs.

### Stage one: capacity clamp

If `threads > cpu_budget`, clamp to `int(cpu_budget)`.

This is unconditional and budget-independent. A hundred threads on a sixteen
core machine is incoherent whether or not memory happens to fit -- it is not a
memory negotiation, it is a request that was never coherent, and the clamp is
honest regardless. `governor.cpu_budget()` returns `psutil.cpu_count()`, so
there is a real number to clamp to.

The clamped value becomes the baseline for stage two. If threads were already
sane, stage one is a no-op and contributes nothing to the card.

**This stage matters more than it looks.** The user launching a hundred-thread
job may simply not know what their machine can do, and the clamp is the most
informative thing on the card -- it teaches a constraint they will hit again.

### Stage two: memory descent

From the post-clamp baseline:

**The feasibility test comes first.** Compute the estimate at the floor
configuration. If even the floor exceeds the budget, no descent can succeed --
return `Infeasible` immediately rather than iterating. This is the index-floor
test the parent issue describes, and it is one `estimate_mb()` call.

**The thread floor is `max(1, baseline // 2)`, computed from the post-clamp
baseline.** This corrects a subtlety in the parent issue, which specifies "half
the original thread count." Half of an incoherent original is still incoherent:
a hundred-thread request would floor at fifty, still not fit, and report
infeasible -- denying a proposal in exactly the case that most needs one.
Halving the *post-clamp* baseline gives 8 for that case, not 50.

The floor exists so a re-plan cannot "succeed" by proposing a single-threaded
forty-hour alignment. That fits the budget and helps nobody.

**Threads halve rather than decrement.** The terms are linear in threads, so a
linear scan buys nothing but iterations.

### Two knobs, and the cheaper one moves first

`sort_memory_mb` multiplies by threads and is usually the dominant term, so it
descends too -- and it descends **first**. Halving the sort buffer costs some
I/O; halving threads costs wall-clock roughly proportionally. The cheaper knob
moves first.

Its floor is the existing `MIN_SORT_MEMORY_MB` in `align_params.py`, so this
does not invent a bound the codebase already has an opinion about.

Order:

1. Descend `sort_memory_mb` toward its floor. If that alone fits, stop --
   threads untouched, no parallelism lost.
2. Otherwise descend threads toward the thread floor, re-checking at each step.
3. If the thread floor is reached without fitting, `Infeasible`.

### Assembly

The same two stages with different knobs. `threads` is the only real one; the
graph term (`genome_bases x bytes_per_genome_base`) is the fixed floor,
structurally the same role the index plays for alignment.

`estimate_assembly_mb()` returning `None` yields **`NoKnobs`, not
`Infeasible`**. That function's docstring is explicit that None is a real
answer rather than a failure -- de novo assembly is what you do when there is
no reference, so a project that cannot supply a genome size is the normal case.
No opinion is not a refusal.

## What the card is told

`Proposal.changes` carries structured before/after pairs:

```
[Change("threads", 16, 8), Change("sort_memory_mb", 1024, 512)]
```

plus `estimate_mb` for the new configuration. That is enough for
[#70](https://github.com/syntheticgio/bioflow/issues/70) to render
"16 threads -> 8, 1024 MB sort buffer -> 512, 14 GB -> 7 GB" without the
backend composing display strings.

`Proposal.note` carries the stage-one clamp sentence when the clamp fired, and
is empty otherwise -- so the card can report "your machine has 16 cores"
distinctly from the memory descent.

### No duration claim, structurally

`timing_service.estimate()` is thread-blind until
[#8](https://github.com/syntheticgio/bioflow/issues/8) lands. A thread-blind
model asked about a thread change reports no change at all, which would be a
confident lie.

**The engine therefore does not call the timing model and has no duration
field.** This is stronger than a note to remember: there is no field for a
caller to read by accident, and none for #8 to have to correct. When #8 lands,
adding a duration is purely additive.

What the card can honestly say is qualitative -- fewer threads will take longer
-- which is a fixed string belonging to #70, not a computed claim.

## Scope

**In scope:** the `replan_service` module, its registry, the verification
wrapper, and `propose()` for alignment and assembly.

**Out of scope:**

- **The card itself** (#70). This engine is band-agnostic: it takes parameters
  and a budget and does not know what called it.
- **Offering re-plan on WARN.** A hundred-thread request is incoherent even
  when memory fits, and `classify()` already returns WARN for
  `threads > cpu_budget` with no proposal attached. Offering the same proposal
  there is right and will be filed as a follow-up -- but it changes the card's
  contract from "recover from a refusal" to "here is a suggestion," and #70
  does not exist yet to have that contract. Because the engine is
  band-agnostic, adding it later changes only the call site.
- **Job splitting.** Named as a future capability, not designed here.
- **Any duration factor**, until #8.

## Ordering against #70

#71 declares #70 a dependency, and for the *user-visible* feature it is: the
button lives on the card. The engine itself has no UI dependency and can be
built and tested standalone. Building it first means #70 has something real to
render rather than a stub.

## Testing

The pure signature -- budgets passed in, no I/O, no machine probing -- is what
makes this testable without a live machine. Per CLAUDE.md, the passing
direction proves little, so the cases that matter assert refusals:

- **An index-dominated job reports `Infeasible`, not a descent to 1 thread.**
  A reference whose index alone exceeds budget, with a reason naming the index.
  This fails if the feasibility test breaks.
- **The hundred-thread clamp.** 100 threads against a 16-core budget yields a
  proposal at <=16 with the clamp noted. This is the case the parent issue as
  written would have gotten wrong.
- **The thread floor holds.** A configuration that would only fit at 2 threads
  from a 16-thread baseline returns `Infeasible` rather than a 2-thread
  proposal.
- **The verification wrapper catches a lying `propose()`.** Register a
  deliberately broken function returning an over-budget proposal; assert the
  engine downgrades it to `Infeasible`. Without this the wrapper is untested
  code that only runs when something else is already broken.
- **The sort buffer descends before threads.** A case that fits by halving the
  sort buffer alone leaves `threads` unchanged.
- **`NoKnobs`** for an unregistered job type, and for an assembly with no
  genome size.
- **Reachability:** every registered entry returns a `Proposal` for at least
  one input.

Backend tests run via `./backend/run-worktree-tests.sh tests/ -q` from a
worktree.

### The gap the unit tests do not close

CLAUDE.md records that green unit tests on hand-built objects have shipped
wrong behaviour in this repo before -- the suggestion rules passed a full suite
while counting `protein.faa` as an alignable reference. The equivalent check
here is a real over-budget configuration against a real reference object, which
`docker compose exec api python -c "..."` can exercise directly before #70's
card exists to show it. That is planned as part of the work rather than left to
fixtures.

## Source

Parent design:
`docs/superpowers/specs/2026-08-07-resource-limits-admission-design.md`
