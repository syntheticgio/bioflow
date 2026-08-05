# Common per-job progress model

One progress payload per job, covering phases, completion, live resource use,
and a derived ETA, written on a single throttled path and read through the two
transports that already exist.

Issue: [#24](https://github.com/syntheticgio/bioflow/issues/24), first
executable slice of epic
[#6](https://github.com/syntheticgio/bioflow/issues/6).

## Why this, and what already exists

Most of the machinery this issue describes shipped some time ago, and the
issue text does not say so. Before designing anything, the inventory:

- `app/models/job.py:83` defines `JobProgress` -- `pct`, `phase`,
  `bytes_done`, `bytes_total`, `message` -- persisted on the job document.
- `app/queue/registry.py:69` defines `JobContext.progress()`, the only
  sanctioned way for a handler to report. Roughly forty call sites use it,
  across trimming, QC, assembly, alignment, variant calling, SRA and NCBI
  downloads, hashing, and ingest.
- `app/queue/executor.py:319` throttles those updates to 2 Hz, with a
  phase-change bypass, then writes them to Mongo and publishes a `job.progress`
  event to Redis.
- `app/api/v1/events.py` bridges Redis pub/sub to SSE per profile;
  `app/api/v1/jobs.py:26` carries `progress` on the job summary. The frontend
  renders a bar in `ActivePipelineJobs.tsx:60`.

So "a common progress model exposed through a backend transport" is, on its
face, done. What is missing is narrower and more specific, and that is what
this design covers: progress that cannot say "I don't know how far along I
am", resource observations that are collected and then discarded, restart
behaviour that has never been decided and is currently wrong, a unit
vocabulary that only speaks bytes, no ETA, and nothing for
[#18](https://github.com/syntheticgio/bioflow/issues/18) to aggregate on.

The backlog entry that produced epic #6 (`docs/TODO.md`, "Observability in
tools") sketches a **separate observability container running a pub/sub
broker** that tools report into and the API queries. That is rejected here.
Redis pub/sub plus the job document already *is* that broker, with a restart
story, a per-profile partition, and a client that works. A second one would be
another container to run, another failure mode to handle, and another set of
decisions to make, to arrive at what is already running. The sketch was
written before the current progress path existed.

## Scope

In:

- Widen `JobProgress`: nullable `pct`, generic units, live resource
  observations, optional phase structure.
- Decide persistence and restart behaviour, and implement the restart part.
- Derive an ETA at emit time from data that already exists.
- Carry run membership on the progress event so #18 can aggregate without a
  per-tick join.

Out, deliberately:

- **DAG aggregation.** This is per-job observability. Aggregate workflow state
  belongs to #18, which may consume what ships here. The boundary is that
  nothing in this design knows what a workflow is; it only makes sure the
  per-job signal carries enough identity to be grouped by something that does.
- **Declaring phase lists for handlers that cannot.** Phase structure is
  optional, and exactly one runner cannot supply it (below). A follow-up issue
  covers that case rather than blocking this one.
- **New instrumentation for tools that report nothing today.** Epic #6 asks
  for representative tool instrumentation slices as separate children. This
  slice changes the model and the transport, not the number of tools that
  report into it.
- **Replacing the resource estimator or the timing models.** `estimate()` is
  consumed here as it is.
- **Enforcement.** Live resource observations are for the user to read.
  Acting on them is the separate "Resource limits and intelligent
  enforcement" backlog entry.

## The model

`JobProgress` gains fields; nothing is removed or renamed.

### Completion

`pct: float | None = None` -- **changed from `float = 0.0`.**

Today `pct` cannot express "unknown", and the cost is visible in the app. The
filter in `JobContext.progress()` drops `None` values, so
`assembly_handlers.py:96` passing `pct=None` does not clear anything: the
field keeps whatever it had, which for a phase-only job is the `0.0` default,
for the entire run. Flye runs for minutes behind a bar reading 0%.

This is not an oversight in the handler. `assembly_runner.py:83` says a
fabricated fraction is worse than an honest phase name, and the same is true
of Clair3 (`variant_runner.py:437`: "any fraction would be invented") and of
minimap2, which `align_runner.py:44` notes says nothing per-batch. For these
tools the honest value is null, and the model needs to be able to hold it.

`None` means indeterminate: render a phase name and an indeterminate bar, not
a bar at zero. Handlers that know a fraction are unaffected.

### Units

`units_done: int | None`, `units_total: int | None`, `unit_label: str = ""`.

`bytes_done`/`bytes_total` stay exactly as they are. They are used by hashing
and chunk assembly, and bytes render differently from countable things -- a
human-readable size, not "3 of 7". Generalizing them would rewrite four call
sites to gain nothing.

The new triple covers what the backlog entry asked for and bytes cannot
express: "N of M chunks", reads, contigs, records. `unit_label` is free text
because the vocabulary is per tool and a controlled list would be a decision
per handler, made now, for handlers that do not exist yet.

### Resource observations

`rss_bytes: int | None`, `cpu_percent: float | None`,
`peak_rss_bytes: int | None`, `peak_cpu_percent: float | None`.

Current and running-peak, both nullable.

`ResourceSampler` already polls the job's process subtree once a second from
the executor, and already tracks running peaks -- see
`queue/resource_sampler.py`. Today every one of those readings is discarded
except the final peaks, which are written to `job_timings` on completion, and
only for runs over `RESOURCE_FLOOR_MS` (60s). A user watching a job that is
about to exhaust their RAM has no way to see it happening.

Both current and peak are carried because they answer different questions.
Current says what the machine is doing now; peak says whether the job has
already touched the ceiling, which is the one a user asks after an
unexplained failure.

The 60-second floor does **not** apply here. That floor exists because
`job_timings` feeds predictive models, and a peak derived from a handful of
samples would be an unreliable input to a fit. A number displayed live is not
an input to anything -- it is what psutil said a moment ago, and for a job
that only runs for thirty seconds that is still the truth about those thirty
seconds. The two consumers want different things from the same sampler, which
is worth stating because a future reader will notice the inconsistency and be
tempted to "fix" it.

### Phase structure

`phase_index: int | None`, `phase_total: int | None`. `phase` stays a free
string.

Where a handler knows its phases up front, these let the UI say "step 2 of 5"
and give #18 something cheap to aggregate. Checking which handlers can
actually supply them:

- **Can**: `fastp_runner` (`_PHASES`, a fixed ordered list),
  `variant_runner` (`_PHASE_PATTERNS`), `align_runner` (aligning, then
  sorting), and every handler with hardcoded `ctx.progress(phase=...)` calls
  -- which is most of them.
- **Cannot**: `assembly_runner.py:97`, and only that one.
  `_STAGE_LABELS.get(stage, stage)` deliberately displays an *unrecognized*
  Flye stage name raw rather than leaving the phase stuck on a stale value.
  Flye's stage list is not closed, so there is no honest `phase_total` to
  declare.

So both fields are optional and null means "unstructured -- render the phase
name alone". That is not a placeholder for a better design; it is the correct
representation for a tool whose stage list is genuinely open. Whether
assembly can be given structure another way (a declared prefix of known
stages, with unknowns appended) is a follow-up issue, deliberately narrow
because the problem is one runner rather than a general gap.

### ETA

`eta_seconds: int | None`, **derived at read/emit time and never persisted.**

Two estimators exist, and the design point is choosing between them per tick
rather than picking one:

1. `timing_service.estimate(job_type, input_bytes)` fits duration against
   input size over prior runs of the same type. It is available at t=0, before
   any progress exists, and it is already served on `GET /jobs/{id}` as
   `timing_estimate`. It is also blind to how the current run is actually
   going.
2. `elapsed / pct` extrapolates from the run itself. It needs a non-trivial
   `pct`, and it self-corrects as the run proceeds.

Prefer (2) once `pct` is known and above a floor of 0.05; fall back to (1);
null when neither is available -- which includes every phase-only job with no
history, and that is the honest answer for those.

The floor matters: at `pct = 0.01` the extrapolation multiplies elapsed time
by a hundred, and the first percent of a job is usually its least
representative (process startup, index loading). Below the floor the
prior-runs model is the better of two poor options.

Not persisted, because a stored ETA is wrong by exactly the time since it was
stored. Deriving it costs one subtraction on a value the caller already has.
The one real cost is that estimator (1) requires a `timing_service` call, so
the SSE emit path takes a Mongo read it did not take before -- see the
throttling note below for why that is bounded.

### Restart

`last_attempt_progress: AttemptProgress | None` on `Job`, not inside
`JobProgress`.

Two cases behave differently today, and only one of them is broken:

- **Terminal failure** already does the right thing. Nothing clears progress
  when a job fails, so a failed job sits at 80% next to its error, which is
  the most useful thing it could show. No change.
- **Requeue and retry** is broken. Nothing in `queue.py` resets progress when
  a lease expires or a backoff fires, so a job that died at 80% comes back
  showing 80% while it restarts from zero. Whatever the bar means at that
  moment, it is not progress.

So: on `mark_running`, if the job carries progress from a previous attempt,
copy `{attempt, pct, phase, message, peak_rss_bytes}` into
`last_attempt_progress`, then reset `progress` to empty. `mark_running` is
already the once-per-attempt Mongo write and already sets `timing.started_at`,
so this adds fields to a write that happens anyway.

Keeping the high-water mark is the point. "Attempt 2; the previous attempt
reached 80% at 'assembly' with a peak of 14.2 GB" is the single most useful
line the UI can show about a job that keeps dying, and it is the shape of a
job hitting an OOM at the same phase every time. Only the previous attempt is
kept, not a list: the interesting comparison is against the last one, and an
unbounded array on a hot document to serve a rarer question is not a trade
worth making here.

### Run membership

`run_ids: list[str]` on the published event -- **a list, not a scalar.**

`run.py:187` records that a `run_id` field on `Job` was explicitly rejected,
because a deduplicated `build_index` job belongs to more than one run: a
second alignment against the same reference reuses the first one's index
build. `RunJob` is a link collection for exactly that reason. A singular
`run_id` on the progress event would re-introduce the bug the model already
rejected, in a place where it would be found much later.

Resolved once per attempt in `mark_running` (the `by_job` index on `run_jobs`
exists) and cached on `JobContext`, the same way and for the same reason
`owner` is -- `registry.py:39` explains it: the throttled writer knows a job
id and an epoch, and must not re-read documents several times a second to
answer "whose stream is this?". The same argument applies verbatim to "which
runs does this belong to?".

This ships now rather than when #18 needs it because the plumbing (a field on
`JobContext`, populated at one site) is the whole change, and doing it later
means touching the same path again with a live consumer attached.

Carrying run ids is the *only* concession this design makes to #18. Nothing
here knows what a run means, aggregates across jobs, or derives a workflow
state. That is the boundary.

## Transport and persistence

**Persisted, on the existing throttled path.** Progress and live resources
both go to the job document via `_write_progress`, and both ride the same
`job.progress` event. No new write path, no second timer, no new transport.

The alternative considered was publishing resources over SSE only, never
persisting them -- cheaper, but a page refresh would blank the numbers on a
running job, and "what is this job doing right now" is precisely the question
asked by someone who just opened the tab. Persistence is what makes the
answer survive a reload, a reconnect, and an API restart.

The cost, stated plainly because it is the tradeoff persistence buys: a
phase-only job like Flye currently writes to Mongo a handful of times per
*run* (phase changes only), and with resource observations it writes on every
throttle tick for the life of the run. At the 0.5s ceiling that is up to two
writes per second per running job. On a single-user local tool with the
governor capping concurrency, this is not a load anyone will notice; on a
hosted multi-tenant service it would deserve a different answer.

The 0.5s throttle stays as the ceiling, and the sampler's 1 Hz tick stays as
it is -- resources ride whatever progress tick follows a sample rather than
scheduling their own writes. The phase-change bypass in `_schedule_progress`
also stays, and `executor.py:322` explains why in detail: a dropped percentage
is superseded by the next tick, a dropped phase is simply lost, and that
distinction was found by watching a real assembly sit at "starting" for six
minutes.

Restart behaviour follows from persistence: after an API or worker restart,
a job's last written progress is what the UI shows, and it is correct as of
the moment it was written. A job that was mid-run when its worker died is
reaped, requeued, and reset by the mechanism above -- so a stale bar
resolves to an empty one with a high-water mark beside it, rather than
sitting at a number that has stopped meaning anything.

Progress remains advisory throughout. `_write_progress` swallows its
exceptions (`executor.py:376`), the write is conditional on the lease epoch so
a resumed zombie cannot clobber a live job's progress, and nothing in the
system makes a decision based on a progress value. That is unchanged and worth
preserving: a progress path that can fail a job is worse than no progress
path.

## API surface

No new endpoints. The two paths that exist both carry the widened model:

- `GET /jobs` and `GET /jobs/{id}` -- `progress` gains the new fields;
  `last_attempt_progress` appears on the detail response. `eta_seconds` is
  computed in the route.
- The `job.progress` SSE event -- same fields, plus `run_ids`.

`GET /jobs/{id}` already returns `timing_estimate` and `memory_estimate` for
non-terminal jobs. Those stay. `eta_seconds` is not a replacement for
`timing_estimate`: the estimate describes what runs of this type usually cost
and carries its own confidence (`samples`, `r_squared`), while `eta_seconds`
is a single number about this run. A UI may reasonably show both, or the
estimate before the job starts and the ETA after.

## Testing

Backend, via `./backend/run-worktree-tests.sh` from this worktree -- not
`docker compose exec api`, which would test main's code.

- `pct=None` survives a round trip through `ctx.progress()`, the executor
  write, and the API read, without being coerced to `0.0` or dropped. This is
  the regression that motivated the change, so it is the test that must fail
  before the fix.
- A phase change bypasses the throttle; a percentage change inside the window
  is dropped. Existing behaviour, but the resource fields ride the same path
  and would be the thing to break it.
- `mark_running` on a job with prior progress stashes and clears; on a
  first attempt it does neither.
- `run_ids` resolves to both runs for a job linked to two, which is the case
  a scalar field would silently get wrong.
- ETA selection: below the floor uses the model, above it uses
  extrapolation, and with neither available returns null.

Per CLAUDE.md, a green suite is not the whole bar. The specific thing to check
against reality rather than fixtures: run a real Flye assembly and a real fastp
trim on the worktree stack (`./ops/worktree-up.sh`, UI on 5273) and watch the
Activity tab. Phase-only jobs should show an indeterminate bar and a live RSS
figure that moves; the fastp job should show a percentage and an ETA that
converges rather than jumping. Both are things a fixture-fed test will report
as working while the app shows a bar stuck at zero -- which is exactly how the
current bug survived.

`docker compose restart worker` before re-testing, since the worker does not
hot-reload and every change here is in its path.

## Follow-ups

- Phase structure for open-ended stage lists (assembly/Flye). Narrow by
  construction: one runner.
- Tool instrumentation slices under epic #6, for tools reporting nothing
  today.
- #18 consuming `run_ids` for aggregate DAG progress.
