# Resource limits as admission, not enforcement

Design for epic [#7](https://github.com/syntheticgio/bioflow/issues/7).
Written 2026-08-07.

## The decision that shapes everything

A user-set resource limit governs **what BioFlow plans to start**, not what it
is allowed to use. Nothing is killed, nothing is capped mid-run, and a job that
overruns its prediction is an accepted outcome rather than a failure.

This was chosen deliberately over container-level cgroup enforcement, which was
the TODO entry's first option. A cgroup limit's entire purpose is the OOM kill,
and an OOM kill twenty minutes into an alignment produces a dead job with a log
that says nothing useful -- the failure mode `resource_estimator`'s own
docstring already exists to prevent. Refusing to *start* work costs a wait;
killing it costs the work.

The consequence is a wording constraint, not just an implementation one. The
limit cannot be presented as "BioFlow will never exceed 16 GB." It means
"BioFlow will not plan to exceed 16 GB." A single mispredicted job will go
over, and if the UI promises otherwise the first overrun reads as a bug.

Cgroup enforcement remains a legitimate thing to want, for someone who would
rather lose a job than a machine. It is out of scope here and stays as its own
issue.

## What already exists

The epic and its TODO entry were written on 2026-08-01 and describe a system
with less in it than the one that exists now. Four things are already built:

**`claim.lua` already gates admission on memory.** It reads `mem_mb` off the
job hash and refuses any candidate where `mem > mem_free`. The Lua half of
admission-by-memory is finished.

**The reservation ledger is already maintained.** `claim.lua` does
`INCRBY bp:conc:mem_mb` on claim; `release.lua:32` does `DECRBY` on release.
Both sides are correct.

**A measured memory model already exists.** `timing_service.estimate_memory()`
fits peak RSS against input bytes per job type, reading through the
outcome-filtered `_modelled()` so an OOM-killed run's peak -- the ceiling it
hit, not what it needed -- never biases the fit downward. It reports
`r_squared` and flags `extrapolating` when asked about an input larger than
anything measured. It is surfaced at `api/v1/jobs.py:256` and consumed by
nothing.

**A heuristic memory estimator already exists and already blocks.**
`pipelines/resource_estimator.py` bands configurations OK/WARN/BLOCK from
coefficients taken from published tool documentation. Its docstring is explicit
that these are not measurements on this hardware.

## The bug in the middle of it

`worker._read_reservations()` reads `bp:conc:cpu` and `bp:conc:io_heavy`. It
does not read `bp:conc:mem_mb`. `compute_free_resources()` has no
`reserved_mem` parameter at all -- `mem_mb` passes through untouched while
`cpu` and `io_heavy` are decremented by their reservations.

So the ledger is written correctly by both Lua scripts and then discarded.
`mem_mb_free` is computed in `_free_resources()` as
`min(psutil available, 70% of budget)`: a snapshot of *actual current* free
memory, which cannot account for jobs already admitted that have not yet
allocated. Two 8 GB alignments claimed within the same second both observe the
full 16 GB free, and both are admitted. Preventing exactly that is why the
reservation exists.

This is why the first slice is the foundation rather than the UI. Every layer
above it inherits this number, and a negotiation UI built on top of a headroom
figure that is still wrong would be confidently wrong.

Nothing catches this today because the reservation counters are only observable
under concurrent claims of memory-heavy jobs, and the failure is
over-admission -- which looks like a busy machine, not like a bug.

## Where the check belongs

Two sites, asking different questions, with different verdicts.

**Enqueue time asks "can this ever fit?"** -- the prediction against the whole
user budget, ignoring current contention. The user is present, in the launch
dialog. This is where a refusal is interactive.

**Claim time asks "does this fit right now?"** -- the prediction against
current headroom. This is `claim.lua`'s existing `mem <= mem_free` test and
needs no new UI. A job that does not fit right now waits, which is correct and
invisible.

Splitting them keeps the interactive part where the human is and leaves
contention as pure queueing.

Two consequences follow, and both remove work:

**No new `JobState` is required.** An enqueue-time refusal happens before the
job document is created, so there is nothing to park in a `needs_review` state
and nothing for the claim scan to learn to skip.

**Child jobs skip the interactive check.** A job with a `parent_job_id` -- a
chained index build, anything in a `depends_on` graph -- has no user watching.
It goes straight to claim-time queueing. Popping a decision card for a job
nobody launched by hand would be a dialog addressed to an empty room.

## The refusal is a negotiation, not a dead end

A BLOCK path already exists at `services/pipeline_service.py:1443`: it raises
`ValidationError` and the alignment is never created. The user gets an error
and starts over from the dialog.

That is the same refusal this design wants, implemented as a dead end. The work
is to give it four exits:

1. **Cancel** -- do not run it.
2. **Edit parameters** -- reopen the dialog with the current settings.
3. **Launch anyway** -- the user knows their machine. Runs unmodified.
4. **Auto re-plan** -- accept a generated configuration that is predicted to
   fit.

**Launch anyway must set a persistent per-job override**, not merely retry.
Without it the job is refused again the moment anything re-queues it. The flag
must survive a requeue after lease expiry, which is the same shape as
`last_attempt_progress` -- per-attempt state that must specifically *not* be
cleared on retry.

**Auto re-plan appears only when a fitting configuration exists.** The
feasibility test runs before the card renders, not when the button is clicked,
so the button is never offered and then refused.

## Where the number comes from

One resolver, shared by the dialog and the enqueue check, preferring the
leftmost source that can answer:

| Layer | Source | Available when |
| --- | --- | --- |
| Measured | `timing_service.estimate_memory()` | >=5 sampled runs of that job type |
| Heuristic | `resource_estimator.estimate_mb()` | Alignment/assembly with a known reference size |
| Declared | `JobResources.mem_mb` | Always |

Only the declared layer feeds admission today.

**The resolver reports its source alongside its number.** "Estimated 14 GB from
23 previous runs" and "Estimated 14 GB from published tool coefficients"
justify different confidence, and the second is what a user is overriding when
they press *Launch anyway*.

**The measured layer must not trust itself outside its observed range.**
`estimate_memory()` already returns `extrapolating` with a `factor_beyond`; a
prediction far past the measured range falls back to the heuristic rather than
refusing a job. Without that guard, five small test runs would confidently
refuse the first real one -- and every row in this database today is test data.

This is what "gets better over time" means concretely: no flag day. Each job
type independently graduates declared -> heuristic -> measured as its rows
accumulate. `resource_sampler.py`'s docstring already states its measurements
are intended to replace `resource_estimator`'s hand-tuned coefficients; this is
that replacement, done per job type as the evidence arrives.

Issue [#8](https://github.com/syntheticgio/bioflow/issues/8) (segment timing
models by thread count) slots in underneath without touching any caller.

## The re-plan algorithm

`resource_estimator.estimate_mb()` already exposes the terms:

```
index       = reference_bases x coefficient      <- fixed, cannot be reduced
per-thread  = threads x bytes_per_thread_mb
sort buffer = threads x sort_memory_mb           <- multiplies; usually dominant
```

The index term is a floor. If the index alone exceeds the budget, no thread
count helps -- that is the feasibility test, and it is cheap.

The search descends over `threads`, **floored well above 1**. A re-plan that
"succeeds" by proposing a single-threaded forty-hour alignment fits the budget
and helps nobody; roughly half the original thread count is the honest floor,
below which it reports infeasible and lets the user choose.

The card states the proposal concretely: *"8 threads -> 4 threads, 14 GB ->
7 GB, roughly 2x longer."* The duration half comes from
`timing_service.estimate()` -- which is thread-blind until #8 lands, so until
then the card says the run will take longer without claiming a factor. A
thread-blind model asked about a thread change reports no change at all, which
would be a confident lie.

## Scope: this slice is the foundation only

**In scope:**

1. Read `bp:conc:mem_mb` in `_read_reservations()` and subtract it in
   `compute_free_resources()`, so the existing ledger reaches the existing
   `claim.lua` gate.
2. A persisted, user-editable global memory limit (and the CPU/thread fields
   #22 calls for).
3. Wire that limit into `_free_resources()` so it replaces the physical-RAM
   budget as the ceiling admission is computed against.

**Out of scope, tracked separately:** the four-choice refusal card, the layered
estimate resolver, the re-plan algorithm, cgroup enforcement, and per-subprocess
`ulimit` (which epic #7 already records as optional and not required here).

The slice is complete and testable on its own: the user sets a limit, and
concurrent admission genuinely respects it. That is a working feature, and it
makes the arithmetic trustworthy for everything layered on later.

## Testing

The reservation fix needs a test that fails against today's code. Per
CLAUDE.md's warning about fixtures that already look the way the code expects,
asserting on hand-built objects is not enough here:

- `compute_free_resources()` is pure. A direct unit test showing that a
  non-zero `reserved_mem` reduces the offered `mem_mb` fails today, because the
  parameter does not exist.
- The over-admission case is the one that matters: two memory-heavy jobs
  claimed against headroom that fits only one. This must assert the second is
  *refused*, not that the first succeeds -- the passing direction proves
  nothing, the same trap CLAUDE.md records for tool-availability tests.
- The in-flight clamp must keep working. `compute_free_resources()` zeroes
  reservations when `in_flight == 0`, which is what makes a leaked counter
  self-healing; memory has to join that clamp rather than bypass it, or a
  missed release permanently shrinks memory capacity until someone restarts a
  worker.

Backend tests run via `./backend/run-worktree-tests.sh tests/ -q` from a
worktree.

## Follow-up issues

Filed as children of #7 so each is independently launchable:

- Read the discarded `bp:conc:mem_mb` reservation (this slice; the bug)
- Persisted global resource limit settings (#22, this slice)
- Layered memory estimate resolver with source provenance
- Four-choice refusal card replacing the `ValidationError` dead end
- Auto re-plan algorithm and feasibility test
- Container-level cgroup enforcement, for hard limits
