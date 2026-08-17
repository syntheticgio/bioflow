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

That machinery bands the wrong number. `classify()` reads `estimated_mb` — a
heuristic from input byte counts and thread count (`resource_estimator.py:35`).
`claim.lua` gates on `job.resources.mem_mb` — a **static per-handler literal**
(`assembly_handlers.py:52` declares `mem_mb=16384`;
`reference_assembly_handlers.py:214` likewise). Nothing compares the declared
literal to the budget. A job therefore passes `Band.OK` — the heuristic says it
fits — and is then permanently unclaimable on its declared number.

Two numbers, two code paths, no check between them. That gap is the bug.

### The budget is 70% of the configured limit

`worker.py:405` applies headroom: `budget_mb = int(budget_source_mb * 0.7)`,
where `budget_source_mb` comes from
`resource_limit_service.resolve_mem_budget_mb(stored_mb=..., machine_mb=...)`.
The value `claim.lua` receives as `mem_budget` is that reduced figure, not the
configured ceiling.

Any launch-time check must compare against the same reduced figure. Comparing
against the full limit would admit jobs in the 70–100% band that the queue then
refuses — the identical failure at a narrower margin, and harder to diagnose
because the two numbers would nearly agree.

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
- **R4** — The check derives its budget from the same source and headroom factor
  the worker applies, so a job that passes the check is claimable on an idle
  machine.
- **R5** — When no `max_mem_mb` is stored, the check uses the machine's own
  memory budget rather than skipping.
- **R6** — A job whose declared `mem_mb` is within the effective budget is
  unaffected: no new refusal, no new warning, no change to its queueing.
- **R7** — The refusal is distinguishable in its wording from the existing
  `Band.BLOCK` estimate refusal, so a user can tell which number is the problem.

## Design

### A shared budget helper

The 0.7 factor currently lives as a bare literal at `worker.py:405`. R4 requires
the launch path to agree with it exactly, and two copies of a magic number that
must agree is the setup for them to silently diverge.

Extract the headroom factor and the effective-budget computation into
`resource_limit_service`, next to `resolve_mem_budget_mb` which already resolves
the stored-vs-machine half:

```python
MEM_HEADROOM_FRACTION = 0.7

async def effective_mem_budget_mb(machine_mb: int) -> int:
    """The budget claim.lua actually gates against."""
```

`worker.py` calls it instead of applying 0.7 inline. The launch path calls the
same function. One definition, two callers, and a test asserting they agree.

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

Both existing `Band.BLOCK` sites gain the new check alongside, ahead of the
estimator call — an exact impossibility should be reported before a heuristic
warning about the same job:

- `pipeline_service.py:~1890` (align_reads)
- `pipeline_service.py:~4243` (assemble)

The declared `mem_mb` is read from the handler's registered `JobResources`, via
the existing registry lookup by job type.

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
| `test_launch_budget_matches_worker_budget` | R4 — both callers of the helper agree |
| `test_no_stored_limit_uses_machine_budget` | R5 — `max_mem_mb=None` still checks |
| `test_within_budget_job_is_unaffected` | R6 — the regression guard |
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
