# Refusing a job that can never be claimed

Design for [#478](https://github.com/syntheticgio/bioflow/issues/478).
Split out of [#457](https://github.com/syntheticgio/bioflow/issues/457), which
made the wait *visible* without deciding what the queue should do about it.

## The bug

`claim.lua:117` admits a candidate on `mem <= mem_free or (override and sole)`,
where `mem_free = math.max(mem_budget - reserved_mem, 0)` (`claim.lua:95`). A
job whose declared `mem_mb` exceeds `mem_budget` itself can never satisfy the
first clause, however idle the machine becomes: draining every other job drives
`reserved_mem` to zero and no further. Compute has no starvation escape
(`governor.py:80-87`, deliberately) and there is no claim timeout, so the job
waits forever.

### Why the existing refusal does not catch it

The launch path already refuses over-budget work and already has a "Launch
anyway" escape: `pipeline_service.py:1898` and `:4249` raise a `ValidationError`
when `resource_estimator.classify()` returns `Band.BLOCK`, unless
`resource_override` is set. That flag rides the job document to `claim.lua` as
the `override` field (`queue.py:471`) and enables the sole-occupancy clause.

The gap is between the number that is *banded* and the number that is
*declared*. `classify()` bands `estimated_mb`. `claim.lua` gates on the job's
declared `resources.mem_mb`. At both launch sites the declared value is computed
separately from the banded one, and nothing compares it to the budget.

The static `JobResources(mem_mb=16384)` literals in the handler modules
(`assembly_handlers.py:52`, `reference_assembly_handlers.py:214`) are
`default_resources` and are **overridden** at both launch sites, so they are not
the cause. The declared values actually enqueued are:

- **align** — `declared_align_mem_mb()` (`pipeline_service.py:1312`), resolved
  through the same estimator plus `memory_estimate.resolve`, floored at
  `MIN_DECLARED_MEM_MB`, and recomputed with `building_index=False`.
- **assemble** — `estimate or UNKNOWN_ASSEMBLY_MEM_MB` (`:4323`), a flat 16384
  when nothing can estimate.

Three ways the declared number escapes the banding, in descending order of how
badly they bite:

1. **The banding is skipped entirely when there is no estimate.** Both sites
   guard on `if estimate is not None:` (`:1888`, `:4241`). An assembly with no
   estimate declares a flat `UNKNOWN_ASSEMBLY_MEM_MB = 16384` having been banded
   by nothing at all. On any budget below ~16 GB it is unclaimable forever, with
   no warning at launch. **This is the cleanest instance of #478**, and the
   reason the new check must sit *outside* that guard.
2. **The `MIN_DECLARED_MEM_MB` floor** can lift the declared value above an
   estimate that banded `OK`.
3. **The align recomputation** with `building_index=False` yields a declared
   number the banding never evaluated.

An exact declared-vs-budget check catches all three, because it reads the value
that is actually enqueued.

### The budget is not the configured limit

`worker.py:405-409` computes what `claim.lua` actually receives:

```python
budget_mb = int(budget_source_mb * 0.7)
return {"mem_mb": max(min(available_mb, budget_mb), 128), ...}
```

Three parts, and only one of them is stable:

- `budget_source_mb` — `resolve_mem_budget_mb(stored_mb, machine_mb)`, the
  configured ceiling. Stable.
- `× 0.7` — headroom so a job overshooting its declaration does not reach swap.
  Stable.
- `min(available_mb, ...)` — a **live** `psutil` reading, floored at 128 MB.
  Moves with whatever else is running.

The live term means a launch-time check **cannot** predict claimability exactly:
the reading it would take is stale by the time the job reaches the head of the
queue. It also means the 128 MB floor can make almost any job *temporarily*
unclaimable under memory pressure — transient, self-resolving, and already
explained by #457's activity view. That is not this bug.

The check therefore compares against `0.7 × budget_source_mb`, the stable part.
Exceeding it means the job can *never* be claimed regardless of machine state,
which is exactly the permanent condition R1 targets. Comparing against the raw
configured ceiling instead would leave jobs in the 70–100% band queueing
forever — the same bug at a narrower margin.

### A prerequisite bug: the worker ignores `hard_mem_mb`

`resolve_mem_budget_mb` accepts a third argument, `hard_mem_mb` — the
kernel-enforced cgroup ceiling, which "binds unconditionally: a soft budget
above it would admit jobs the kernel then kills"
(`resource_limit_service.py:27-30`). `worker.py:392-394` does not pass it.

So the admission budget can currently sit above the kernel's hard limit, and
jobs are admitted that the kernel then OOM-kills. This work depends on the
figure being correct — a launch check passing `hard_mem_mb` while the worker
omits it would have the two budgets disagree — so it is fixed here, in its own
commit, ahead of the feature.

## Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | Refuse at launch when declared `mem_mb` exceeds the effective budget | The job cannot be claimed; queueing it is a lie |
| D2 | Reuse `resource_override` as the escape, do not auto-set it | The user's explicit "Launch anyway" is a better signal than an inference |
| D3 | A new check in the launch path, not an extension of `classify()` | Keeps the heuristic estimate and the declared literal distinct, with distinct wording |
| D4 | Check against the effective budget, covering both a user-set `max_mem_mb` and the machine's own memory | `max_mem_mb` is `None` on a fresh install, where the bug is still reachable |
| D5 | Leave `classify()`'s `mem_budget_mb is None → Band.OK` policy alone | Correct for a heuristic; the new check needs no such fallback (see R4) |
| D6 | A conservative `max_mem_mb` may make assembly permanently unlaunchable | That is the honest consequence of the setting; the refusal names it and offers the override |

D6 is a deliberate, user-visible behaviour change. Someone who set an 8 GB
budget and has been running assemblies (declared 16384 MB) will now see a
refusal where the job previously queued. It previously queued and never ran, so
nothing that worked stops working — but the failure moves from silent to loud,
which is the point.

## Requirements

Each is checkable, has one obligation, and names its actor.

- **R1** — When a user launches a pipeline job whose declared `mem_mb` exceeds
  the effective memory budget, and `resource_override` is not set, the API
  refuses the launch with a `ValidationError` rather than creating a job.
- **R2** — The refusal message states the declared requirement, the effective
  budget, and the setting that governs it, each as a number the user can act on.
- **R3** — When the same user retries with `resource_override=True`, the launch
  succeeds and the created job carries `resource_override=True`, so `claim.lua`
  admits it under sole occupancy.
- **R4** — The check derives its budget from the same configured source and
  headroom factor the worker applies, so a job it refuses is one `claim.lua`
  could never admit. It deliberately excludes the worker's live `available_mb`
  term: a job that passes the check may still wait under transient memory
  pressure, which #457 already explains and this work does not address.
- **R4a** — The worker's admission budget accounts for `hard_mem_mb`, so it
  never exceeds the kernel-enforced cgroup ceiling.
- **R5** — When no `max_mem_mb` is stored, the check uses the machine's own
  memory budget rather than skipping.
- **R6** — A job whose declared `mem_mb` is within the effective budget is
  unaffected: no new refusal, no new warning, no change to its queueing.
- **R6a** — An assembly for which no memory estimate can be produced, declaring
  `UNKNOWN_ASSEMBLY_MEM_MB`, is still checked and still refused when that value
  exceeds the budget — the existing banding is skipped entirely in this case.
- **R7** — The refusal is distinguishable in its wording from the existing
  `Band.BLOCK` estimate refusal, so a user can tell which number is the problem.

## Design

### A shared budget helper

The 0.7 factor currently lives as a bare literal at `worker.py:405`. R4 requires
the launch path to use the same factor, and two copies of a magic number that
must agree is the setup for them to silently diverge.

Extract the factor and the stable part of the computation into
`resource_limit_service`, next to `resolve_mem_budget_mb` which already resolves
the stored-vs-machine half:

```python
MEM_HEADROOM_FRACTION = 0.7

def admission_budget_mb(*, stored_mb: int | None, machine_mb: int,
                        hard_mem_mb: int | None = None) -> int:
    """The stable ceiling admission plans against, before live headroom.

    `worker._resource_budgets` further clamps this by a live `available_mb`
    reading; the launch-time check deliberately does not. See the spec's
    "The budget is not the configured limit".
    """
```

`worker.py` calls it and then applies its live clamp to the result, rather than
applying 0.7 inline. The launch path calls the same function and uses it
directly. One definition of the stable ceiling, two callers, and a test
asserting the worker's budget never exceeds it.

### The check

A new function in `resource_estimator`, exact rather than heuristic, kept
separate from `classify()` per D3:

```python
def exceeds_declared_budget(*, declared_mb: int, budget_mb: int) -> bool:
    return declared_mb > budget_mb
```

Trivial as a predicate; its value is the name and the single place the
comparison lives. Paired with an `explain_declared_refusal()` producing R2's
message, distinct in wording from `explain()` per R7 — this one reports a fixed
requirement, not an estimate, so it says "requires" rather than "estimated" and
names no input sizes.

### Call sites

Both launch paths gain the new check, and **outside the `if estimate is not
None:` guard** that wraps the existing banding. Case 1 above is exactly a job
with no estimate, so a check placed inside that guard would miss the clearest
instance of the bug.

The check runs on the value that will actually be enqueued:

- `pipeline_service.py` align — the result of `declared_align_mem_mb(...)`,
  computed at `:2058` and passed to `enqueue` at `:2085`. The check moves after
  `:2058` and before the enqueue.
- `pipeline_service.py` assemble — `estimate or UNKNOWN_ASSEMBLY_MEM_MB`,
  currently inline at `:4323`. Hoist it to a local before the enqueue and check
  that local.

Reading the enqueued value rather than the handler's `default_resources` is what
makes the check correct: the defaults are overridden at both sites.

No `replan` proposal accompanies this refusal. `replan_service` proposes lower
thread counts, which cannot help: the declared literal is static and does not
scale with threads. Offering a replan that changes nothing would be worse than
offering none.

### What is deliberately not done

- **No thread downscaling.** `mem_mb` is a static literal with no
  memory-per-thread model behind it. Building one is a feature, not this fix.
- **No compute starvation escape.** `governor.py:80-87` argues against it and
  that reasoning holds. It also would not fix this: the governor gates *class*
  admission, while this job is stuck on the *memory* gate inside `claim.lua`.
  Granting the escape leaves the job waiting exactly as long.
- **No check at `enqueue()`.** It would catch more callers, but by then the
  user's launch dialog is gone and there is nowhere to offer "Launch anyway".

## Correcting a stale comment

`worker.py:383-385` reads:

> This is the entire enforcement path for the setting: `claim.lua` already
> refuses any candidate whose declared mem_mb exceeds the live-computed free
> amount, so a smaller ceiling here *is* the limit taking effect.

That describes this bug as though it were the design. After this change the
enforcement path is the launch-time refusal, with `claim.lua` as the backstop.
Update it in the same commit — the comment is load-bearing for the next person
reasoning about where the limit binds.

## Testing

Backend only; no UI change beyond the message text the existing refusal card
already renders.

| Test | Asserts |
|---|---|
| `test_declared_over_budget_is_refused` | R1 — launch raises, no job created |
| `test_refusal_names_both_numbers` | R2 — declared and budget both in the message |
| `test_override_admits_the_job` | R3 — job created, `resource_override is True` |
| `test_worker_budget_never_exceeds_admission_budget` | R4 — the worker's live clamp only lowers the shared ceiling |
| `test_hard_mem_mb_lowers_the_worker_budget` | R4a — the kernel ceiling binds |
| `test_no_stored_limit_uses_machine_budget` | R5 — `max_mem_mb=None` still checks |
| `test_within_budget_job_is_unaffected` | R6 — the regression guard |
| `test_unestimatable_assembly_is_still_refused` | R6a — the check runs outside the `estimate is not None` guard |
| `test_declared_refusal_differs_from_estimate_refusal` | R7 — wording is distinguishable |

Per CLAUDE.md's note on registries and real data, one check beyond the unit
tests: run a real `assemble` launch against a deliberately low `max_mem_mb` on
the worktree stack and confirm the refusal appears in the UI with both numbers,
rather than a job that queues and sits. The unit tests feed hand-built objects
that already look the way the check expects; this is the step that catches a
budget resolved from the wrong source.

## Verification

- `./backend/run-worktree-tests.sh tests/ -q` green from the worktree.
- A low `max_mem_mb` refuses `assemble` at launch, naming both numbers.
- The same launch with "Launch anyway" creates a job that runs on an idle
  machine rather than waiting forever.
- A normal-sized job launches unchanged.
