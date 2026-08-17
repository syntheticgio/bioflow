# Explaining why a queued job is not running

**Issue:** [#457](https://github.com/syntheticgio/bioflow/issues/457)
**Date:** 2026-08-17
**Status:** design

## The report

A user launched a Flye assembly and watched the Activity view sit at:

```
IN PROGRESS   1 run · 1 job
WAITING · STARTED 12:42:42 PM
Assemble SRR37688468.fastq.gz
0 OF 1 JOB          [Cancel run]
QUEUED   assemble                    12:42:42 PM
```

> I don't know what this is waiting on - is it waiting on resources? Are there
> jobs that are running to support this, etc.

Every word on that card describes *that* the job is waiting. Nothing describes
*why*. The two questions the user asked -- is this resources, or is something
else running first -- are exactly the two the queue already knows the answer to
and does not say.

## Diagnosis

This is not a missing feature. The reason is computed, and then discarded.
Three defects compound.

### 1. `claim.lua` throws the answer away

`backend/app/queue/scripts/claim.lua` decides dispatchability with four
independently-failing gates:

```lua
local fits = allowed[class]
             and cpu <= cpu_free
             and mem_ok
             and (io ~= 'heavy' or io_free > 0)
```

When no candidate fits, the script returns `nil`. Which gate failed, and the
two numbers on either side of the comparison, are in hand at that moment and
are dropped on the floor. This is the only place in the system where the
question is answerable authoritatively: the script reads the live `bp:conc:*`
counters inside its own atomic execution precisely because any value computed
outside it is a stale snapshot. Any "why is this waiting" answer derived
anywhere else is a second opinion that can disagree with the real decision.

### 2. The run card never asks

`frontend/src/lib/runFormat.ts:185` already has a `waitingReason(job, load)`
that produces "waiting: system loaded", "waiting: system busy", or "waiting for
a free slot". It is wired into `JobRow` in the *"Other waiting"* loose-job
section of `ActivityView.tsx` (the `load` prop at line 206).

The user's job was not loose. It belongs to a run, so it renders through
`activity/ActivityLead.tsx`, which is never passed `load` and never calls
`waitingReason`. Run-owned jobs -- which is to say, every job a user launches
deliberately -- show a bare `WAITING`/`QUEUED`. That is the screenshot.

### 3. The governor bets on a UI that does not deliver

`backend/app/queue/governor.py:80-87` denies compute jobs the starvation escape
that maintenance jobs get, and justifies it in a comment:

> Compute does not qualify: a waiting pipeline run is *visible* as waiting in
> the activity view, so it fails loudly rather than silently

That premise is false, per defect 2. The consequence is not cosmetic: a job
whose declared `mem_mb` exceeds the machine's whole memory budget can never
satisfy `mem <= mem_free`, has no starvation escape, and no timeout. It waits
forever, silently. The comment is load-bearing reasoning for a policy choice
and is currently wrong.

## What the current data model cannot express

- `JobResources` (`backend/app/models/job.py:66`) holds the declared demand
  (`cpu`, `mem_mb`, `io`), but `JobSummary` in `frontend/src/api/types.ts:383`
  does not carry it. The frontend cannot compare demand against headroom.
- `run_service.status_for` (`backend/app/services/run_service.py:223-236`)
  serializes `job_id`, `role`, `shared`, `type`, `state`, `progress`, `error`,
  `created_at` -- no resources, no reason. This is the payload behind the run
  card in the screenshot.
- `SystemLoad` (`frontend/src/api/types.ts:454`) carries budgets and current
  usage but nothing per-job.

## Requirements

Identifiers are permanent and are not reused.

### Reporting the reason

**R1.** When `claim.lua` completes a scan without claiming, it must record the
blocking gate for the highest-priority candidate it skipped.

**R2.** The recorded reason must name exactly one gate: `class`, `cpu`, `mem`,
or `io`. Where several gates fail at once, the recorded gate is the first in
that fixed order, so the same queue state always produces the same reason.

**R3.** The recorded reason must carry the demand and the free headroom for
that gate, as integers in the gate's own unit (cores for `cpu`, MB for `mem`).
The `class` gate carries the job class and the admitted-class list instead.

**R4.** The reason must be recorded only for the head-of-queue candidate, not
for every scanned candidate, so the cost of recording is O(1) per claim attempt
regardless of queue depth.

**R5.** A recorded reason must expire without being overwritten. A stale reason
must never be presented as current.

**R6.** Recording a reason must not change which job `claim.lua` selects, nor
whether it selects one.

### Presenting the reason

**R7.** A user viewing a waiting job in a run card must be able to read which
gate is blocking it and the two numbers from R3, without leaving the Activity
view.

**R8.** When no fresh reason is available, the view must fall back to the
existing inference from `waitingReason()` rather than showing nothing or
showing a blank reason.

**R9.** A waiting job that belongs to a run must present its reason with the
same words as an equivalent loose job. One vocabulary, two render paths.

**R10.** A job waiting because an earlier job in its run has not finished must
say so, and must not be reported as waiting on resources.

### The unsatisfiable case

**R11.** When a job's declared `mem_mb` exceeds the total memory budget (not
merely the currently-free headroom), the view must state that the job cannot
start on this machine, distinctly from a job that is waiting its turn.

**R12.** The unsatisfiable message must name the declared demand and the total
budget.

**R13.** A user reading an unsatisfiable-job message must be offered the
existing `resource_override` ("Launch anyway") affordance, reusing the
`Band.BLOCK` refusal card already presented at launch time
(`pipeline_service.py:1888-1891`).

**R14.** An unsatisfiable job must remain cancellable from the same card.

### Non-functional

**Performance.** The added Redis work per claim attempt is one write of a small
fixed-size value, only on the no-claim path. Claim latency at the 95th
percentile must not regress measurably against the current implementation.

**Capacity.** One reason key per node, bounded by a short TTL. No growth with
queue depth or job count.

**Consistency.** The presented reason must be the decision the queue actually
made, not a re-derivation. This is why R1 places recording inside `claim.lua`.

**Accessibility.** The reason is text, not conveyed by color alone. A gate
shown in a warning color must read as a sentence with the color removed.

## Design

Three layers. Each is independently useful and independently shippable.

### Layer 0 -- wire the run card up

Pass `load` into `ActivityLead` and call the existing `waitingReason()` for
run-owned waiting jobs. Satisfies R8, R9, and R10 on its own, using only
machinery that already exists, and turns the reported blank `WAITING` into a
sentence. Lands first, as its own commit, because it is small and it is the
literal fix for the screenshot.

### Layer 1 -- the queue reports why

At the point where `claim.lua` evaluates `fits` for the first candidate, record
the failing gate and its two numbers to a per-node Redis key with a short TTL.
The values are already local to the script; this is a single `SET ... EX` on
the path that currently returns `nil`.

Recording only for the first candidate (R4) is deliberate. It keeps the write
O(1), and it makes the message describe the job that is actually next in line
rather than an arbitrary one deep in the queue.

The fixed gate order (R2) is `class`, `cpu`, `mem`, `io`: class first because
governor closure explains every job at once and is the least specific to this
job; `mem` before `io` because memory is the gate that produces the
unsatisfiable case in R11.

### Layer 2 -- surfacing it

Extend `run_service.status_for`'s per-job dict with the declared resources and
the current blocking reason, and extend `JobSummary` to match. `waitingReason()`
gains a branch that prefers a fresh recorded reason and falls back to today's
inference (R8), so the two render paths keep one vocabulary (R9).

The unsatisfiable check (R11) compares declared `mem_mb` against the *total*
budget from `SystemLoad.memory.budget_bytes`, not against free headroom. Free
headroom recovers as jobs finish; the total budget does not, which is what makes
the two cases categorically different and worth different words.

## Testing

- **`claim.lua` gate attribution** -- against a real Redis, drive each gate to
  fail in isolation and assert the recorded reason names that gate with the
  right numbers. Also assert the all-gates-fail case picks the R2 order, and
  that recording does not change selection (R6).
- **`waitingReason()`** -- a pure function; table-driven over fresh reason,
  stale reason, absent reason, blocked-on-predecessor, and unsatisfiable.
- **Against the real database, not only fixtures.** Per CLAUDE.md's note on the
  Actions-tab rules, hand-built objects that already look the way the code
  expects are exactly what hid the last bug of this shape. Launch a genuinely
  over-budget assembly on the worktree stack and read the card.

There is no headless component-testing setup in this repo, so the render paths
are verified manually at the worktree stack's UI on localhost:5273.

## Out of scope -- to be filed separately

1. **Auto-resolving an unsatisfiable job.** This design makes the forever-wait
   *visible* and offers the override. Whether the queue should additionally
   auto-fail such a job, downscale its thread count, or grant compute a
   starvation escape is a queue-policy decision with its own trade-offs, not a
   UX one.
2. **Correcting `governor.py:80-87`.** The comment's premise becomes true only
   when Layer 0 lands. It should be rewritten then, not left asserting
   something the code no longer relies on being false.

## Decisions and their reasons

| Decision | Why |
|---|---|
| Record inside `claim.lua` rather than probing separately | A separate prober reads the counters outside the atomic execution, so it can disagree with the real decision. The script's own docstring makes this argument about caller-supplied snapshots. |
| Record head-of-queue only | O(1) cost, and it describes the job actually next in line. |
| Fixed gate order rather than reporting all failures | Two people reading the same queue state get the same sentence; "waiting on cpu, mem, io" is not more useful than "waiting on cpu". |
| Keep the inference fallback | Redis being cold must degrade to today's behaviour, not to a blank. |
| Unsatisfiable compares against total budget | Free headroom recovers; total budget does not. Different recovery story, different words. |
| Layer 0 ships alone first | It is the literal fix for the reported screenshot and depends on nothing else. |

**Source:** reported by @syntheticgio in #457 on 2026-08-17, from a Flye
assembly run that sat in `WAITING` with no explanation.
