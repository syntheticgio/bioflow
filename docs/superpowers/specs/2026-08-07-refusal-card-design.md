# The four-choice refusal card

Design for [#70](https://github.com/syntheticgio/bioflow/issues/70), a child of
epic [#7](https://github.com/syntheticgio/bioflow/issues/7).
Written 2026-08-07.

Depends on [#71](https://github.com/syntheticgio/bioflow/issues/71)
(`replan_service`, merged and callerless) and the layered estimate resolver
(`memory_estimate.resolve`, merged). Both are built; this is the work that
gives them a caller.

## What is actually being replaced

The parent design
([`2026-08-07-resource-limits-admission-design.md`](2026-08-07-resource-limits-admission-design.md))
describes the BLOCK path as a dead end: the user presses Launch, gets a
`ValidationError` toast, and starts over from the dialog.

That is accurate for **assembly**. It is not accurate for **alignment**, and
the difference shapes the whole design.

`AlignDialog` already runs the same arithmetic client-side (`lib/estimate.ts`,
against coefficients from `/pipelines/align-envelope`) and **disables the
Launch button** while `band === "block"`, showing a red banner reading "Reduce
threads or sort memory, or choose an aligner with a smaller index." So the
`ValidationError` at `pipeline_service.py:1486` is nearly unreachable from the
UI. The alignment dead end is a *disabled button*, not an error toast.

The assembly refusal at `pipeline_service.py:3279` has no envelope endpoint and
no client mirror, so it genuinely is the reactive 422 the parent design
describes.

**Two BLOCK sites, two different current experiences, one card.** The issue
names only the alignment site; both are in scope.

## Where the card is triggered

Approach: **the card is a shared component with two different triggers**, one
per dialog, matching what each path already knows.

**Alignment: pre-flight.** The dialog already computes the band before any
request is sent. On `block`, the card replaces the disabled Launch button and
the banner, in place. Nothing is sent to the server to produce it.

**Assembly: reactive.** The dialog catches the 422 and renders the same card
from the error's `details`.

The alternative -- giving assembly its own envelope endpoint and a client
mirror of `estimate_assembly_mb` so both paths are pre-flight -- was rejected.
`estimate.ts`'s own docstring calls the duplicated arithmetic "a real cost,"
and a second copy doubles it to make one dialog's refusal arrive marginally
sooner. The reactive path also has a property the pre-flight path does not: it
exercises the server's authoritative check for real, so the card still appears
for a direct API caller, and for the case where the dialog's envelope went
stale between opening and launching.

The cost is honest and small: assembly pays one round trip before the card
appears. A pre-flight variant for assembly can be added later without touching
the card.

## The card

One component, `ResourceRefusalCard`, fed by a props shape both dialogs
produce:

```
estimateMb, budgetMb, estimateSource, detail, explanation
replan: Proposal | Infeasible | NoKnobs | null
onCancel, onEdit, onLaunchAnyway, onAcceptReplan
```

### Naming the estimate source

The card states the source in one line, taken from `resolved.detail`, which
already reads as prose:

- "Estimated 14,232 MB from 23 previous runs on this machine"
- "Estimated 14,232 MB from published tool coefficients"

This is an acceptance criterion, and its purpose is decision-relevant rather
than diagnostic: the second sentence is what a user is overriding when they
press *Launch anyway*, and it deserves less deference than the first.

**`r_squared` is deliberately not shown.** `resolve()` already falls back to
the heuristic when a measured estimate extrapolates too far past its observed
range, so any measured number that reaches the card is inside its own guard
rails. Adding a goodness-of-fit statistic to a decision card shows a number to
someone deciding whether to press a button, when the source name is the
actionable part.

### The four exits

**Cancel** dismisses the card and the dialog. Nothing runs.

**Edit parameters** dismisses the card and returns to the dialog's fields. In
`AlignDialog` this is nearly free: `showAdvanced` is already forced open while
the band is not `ok`, so the threads and sort-memory fields the card points at
are already visible.

**Launch anyway** sends the launch with a persistent override (below). It
carries its consequence inline rather than behind a confirmation step -- see
"Stating the consequence" below.

**Auto re-plan** appears **only** when the replan result is a `Proposal`. This
is the parent design's constraint that the button is never offered and then
refused. `Infeasible` renders its `reason` as prose instead; `NoKnobs` renders
nothing extra. Per #71, the two are distinct on purpose: "nothing fits" and
"there is nothing here to tune" call for different next steps.

A `Proposal` displays `changes` as a knob diff (threads 16 → 8,
sort_memory_mb 1024 → 256) and `note` as its own separate line. #71's
implementation notes are explicit that the clamp sentence -- "100 threads is
more than this machine can run; it has 16 cores." -- is often the more useful
line for a user who over-requested threads without knowing their hardware, and
collapsing it into the knob diff loses that.

**No duration factor is claimed.** `timing_service.estimate()` is thread-blind
until [#8](https://github.com/syntheticgio/bioflow/issues/8), and a
thread-blind model asked about a thread change reports no change at all. The
card says qualitatively that fewer threads means a longer run.

### Accepting a proposal fills the form; it does not launch

`onAcceptReplan` writes the proposed params into the dialog's `overrides`,
dismisses the card, and lets the band recompute -- which, since every
`Proposal` is verified against the same estimator that produced the refusal,
returns `ok`. The user then presses Launch themselves.

This keeps every path through the card converging on the one Launch button
rather than creating a second way to start a job. The `changes` list already
served as the preview, so landing in a form showing the new values is
confirmation rather than surprise; and it makes "Auto re-plan" and "Edit
parameters" differ only in who filled the fields, so a user can nudge a
re-planned value before committing to it. A terminal "Run with 8 threads"
button would be one click faster and offer no undo once the job is queued.

### Stating the consequence

*Launch anyway* commits the user to something non-obvious: the job will be
admitted **only when nothing else is running**, and it may exceed the
configured memory limit. A user whose overridden job then sits behind two
others would reasonably read that as broken.

So the card states this where the button is, rather than behind a second
click. A confirmation step would put the most friction on the least-used exit,
and the card is already the confirmation -- the user has read the estimate, the
budget, and the source before reaching the button.

The wording must not imply a safety net that does not exist. After the
foundation slice, the budget admission is computed against is the **user's
configured limit**, not physical RAM. An override therefore relaxes a
preference, and if that preference was set above the machine's physical
memory, the job can still exhaust it. This is consistent with the parent
design's "admission, not enforcement" decision, but the card should not read
as a guarantee.

## The re-plan endpoint

`POST /pipelines/replan`, taking `job_type` and `params`, returning a tagged
union mirroring `ReplanResult`.

**`budget_mb` and `cpu_budget` are resolved server-side** from `LoadGovernor`,
not accepted from the client. A client that states its own budget can state a
larger one, which would turn the feasibility test into a formality.

`replan_service` already verifies every `Proposal` against the same estimator
that produced the refusal before returning it, degrading a miscomputing
per-type function to `Infeasible` rather than to an offered-then-refused
button. The endpoint adds no verification of its own.

Alignment calls this when the card opens. Assembly does not call it separately
-- the result is inlined into the 422 body, so the card has everything it needs
without a second round trip.

Registered job types today are `JOB_TYPE_ALIGN_READS` and
`JOB_TYPE_ASSEMBLE`, both string constants defined on `replan_service` itself.
The import direction is one-way: `pipeline_service` imports `replan_service`,
never the reverse, to avoid the circular import #71 already worked around.

## The override flag

### Why a persisted flag rather than a retry

Without persistence the job is refused again the moment anything re-queues it.
The flag must survive a requeue after lease expiry -- the same shape as
`last_attempt_progress` (`models/job.py:187`), per-attempt state that must
specifically *not* be cleared on retry.

But the flag's more important job is downstream, and the issue does not spell
it out. **The enqueue-time check is the only place a BLOCK refusal happens.**
A requeue after lease expiry goes back through the claim scan, not through
`launch_alignment()`, so a flag defending only against re-refusal would be
defending a path nothing traverses.

What the flag actually prevents is a *worse* dead end. An overridden job is by
construction one whose `mem_mb` exceeds the budget, so `claim.lua`'s
`mem <= mem_free` gate would refuse to claim it forever. Launch anyway without
a claim-time effect produces a permanently queued job -- strictly worse than
the refusal it replaced, and silent.

### What it relaxes

`claim.lua` relaxes **only the memory gate**, and **only when the job would be
the sole occupant**:

```lua
-- HMGET grows from five fields to six:
--   'class', 'cpu', 'mem_mb', 'io', 'epoch', 'override'
local override = h[6] == '1'
local sole = reserved_cpu == 0 and reserved_mem == 0 and reserved_io == 0
local mem_ok = mem <= mem_free or (override and sole)
```

`h[6]` is the appended field, so the existing `h[1]`..`h[5]` reads keep their
positions and nothing downstream shifts. One field added to the existing
`HMGET`, one boolean. No new Redis reads, no
extra round trip, atomicity unchanged.

This matches what a user overriding the check actually wants: run it, accept
the risk, but do not stack it against three other jobs. It keeps the
reservation ledger honest -- no false `mem_mb` declaration -- and it means an
overridden job can never be the cause of a multi-job overcommit. The budget is
a user preference; the physical RAM ceiling is not, and relaxing the first
while respecting the second is the whole point.

**CPU and `io_heavy` gates are unchanged.** `classify()` bands a CPU
overcommit to WARN, not BLOCK, so a thread count over budget never produces a
refusal card in the first place. The override stays scoped to the thing that
was actually refused.

### The `ignore_reservations` trap

`claim.lua` takes `ignore_reservations` (ARGV[9]), the caller's in-flight
self-healing clamp: a worker with nothing running cannot still owe a
reservation, so when it is set the counters are **not read at all** and the
full budget is offered.

In that branch `reserved_cpu`, `reserved_mem` and `reserved_io` are all zero
because *nothing was read*, not because nothing is running. Computing `sole`
from those variables would report "sole occupant" whenever the claiming worker
happens to be idle -- which, with more than one worker, is a different claim
entirely, and makes the override strictly more permissive than an unconditional
exemption while reading in the source as if it were more conservative.

**The `sole` computation must come from a real `MGET`**, performed even when
`ignore_reservations` is set, or the override branch must be gated on
`not ignore_reservations`. Either is acceptable; doing neither is the bug.

### Where it lives

- `Job.resource_override: bool = False` on the job document, persisted.
- Mirrored onto the Redis job hash as `override` at enqueue.
- `launch_alignment()` and the assembly launcher accept `resource_override:
  bool` and skip the BLOCK raise when it is set.
- Survives requeue because the reconciler rebuilds the hash from MongoDB,
  which is the record of truth.

## Child jobs: a test, not a guard

The issue's acceptance criterion "jobs with a `parent_job_id` never render a
card" **is already true**, and not because of anything this design adds.

`parent_job_id` is set inside `queue.enqueue()` (`queue/queue.py:68,134`) by
callers in `queue/results.py` -- jobs spawned by an already-running job. The
BLOCK checks live in `launch_alignment()` and the assembly launcher, both
reached only from the API, where no `parent_job_id` is ever passed. A child job
does not traverse the refusing code at all.

Adding an `if job.parent_job_id: skip` guard would be dead code defending a
path that does not exist, and worse, it would read as though the two entry
points were shared when they are not.

**This is covered by a regression test rather than a branch.** The property is
real and worth pinning; it just is not implemented by this change.

## Testing

Per CLAUDE.md's warning about fixtures that already look the way the code
expects, the assertions below are chosen for the direction that fails when a
seam breaks.

- **Override refused under contention.** An overridden job with another job
  holding a reservation must **not** be claimed. This is the direction that
  fails if `sole` is computed wrongly; asserting the admitted case proves
  nothing, since the image admits most things.
- **Override admitted when alone.** The complementary case, with all three
  counters at zero from a real read.
- **The `ignore_reservations` interaction, specifically.** An idle worker must
  not read as sole occupant. This is the trap above and needs its own test, not
  coverage as a side effect of another.
- **Override survives a lease-expiry requeue.** Assert on the rehydrated Redis
  hash, not only the MongoDB document -- the document surviving is the easy
  half, and the hash is what `claim.lua` reads.
- **Child jobs never reach a BLOCK check.** A job enqueued with a
  `parent_job_id` traverses no refusing code.
- **The replan endpoint returns each of the three variants**, and the card's
  props logic omits the button for `Infeasible` and `NoKnobs`.

There is no headless component-testing setup in this repo and none is expected,
so the card itself is verified manually in the browser. From a worktree that is
`./ops/worktree-up.sh` (UI on 5273), and backend tests run via
`./backend/run-worktree-tests.sh tests/ -q`.

Worth doing beyond the suite, per CLAUDE.md's note about rules that pass their
unit tests while being wrong about real data: open the card against a real
project whose reference genuinely over-budgets, and confirm the estimate source
line names what actually produced the number.

## Out of scope

- A pre-flight envelope for assembly (`assemble-envelope` plus a client mirror
  of `estimate_assembly_mb`). The reactive path is sufficient and cheaper.
- Any duration factor on a re-plan proposal, until #8 makes
  `timing_service.estimate()` thread-aware.
- Relaxing the CPU or `io_heavy` gates under override.
- Cgroup enforcement, which is its own issue and a different philosophy.
