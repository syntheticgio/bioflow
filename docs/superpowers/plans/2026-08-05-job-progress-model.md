# Common per-job progress model — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A running job says what it is doing, how far along it is *or* honestly that it does not know, how much memory and CPU it is using right now, and roughly how long is left — and a job that died and got requeued says so instead of showing a stale bar. Implements [#24](https://github.com/syntheticgio/bioflow/issues/24), the first executable slice of [epic #6](https://github.com/syntheticgio/bioflow/issues/6).

**Architecture:** Widen the existing `JobProgress` and the one throttled write path in `queue/executor.py` that feeds it. No new transport, no new endpoints, no new container. The resource sampler that already polls at 1 Hz becomes the thing that drives progress ticks for jobs that report no percentage of their own; `mark_running` becomes the once-per-attempt place where progress is reset, run membership is resolved, and the duration model is consulted.

**Tech Stack:** FastAPI + Beanie + Pydantic v2, Redis pub/sub → SSE, psutil for sampling. React 18 + TypeScript + TanStack Query on the frontend. No new dependencies.

**Design spec:** `docs/superpowers/specs/2026-08-05-job-progress-model-design.md` — read it first, particularly "Why this, and what already exists". Most of the progress machinery this plan touches already ships; the spec inventories it so you do not build a second one alongside.

**Out of scope:** DAG aggregation ([#18](https://github.com/syntheticgio/bioflow/issues/18) — this plan only carries `run_ids` so that issue has something to group on), new instrumentation for tools that report nothing today (separate children of epic #6), phase structure for Flye's open-ended stage list (follow-up, see Task 7), and any enforcement based on the resource numbers (the separate "Resource limits" backlog entry).

---

## Before you start

### Read these three things

1. `queue/executor.py:319` — `_schedule_progress`, and its docstring about why a
   phase change bypasses the throttle. That reasoning was paid for by a real
   assembly sitting at "starting" for six minutes; do not undo it.
2. `queue/registry.py:32` — `JobContext`, especially the comment on `owner`
   explaining why it is carried on the context rather than looked up. Tasks 4
   and 5 add two more fields for exactly that reason.
3. `models/run.py:187` — why a scalar `run_id` on `Job` was rejected. Task 4
   would silently recreate that bug if you make `run_ids` a string.

### Baseline

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Record the count before touching anything. Use this script, **not** `docker
compose exec api` — from a worktree that command tests main's code and every
result describes the wrong tree (`CLAUDE.md`, "Verifying changes").

Measured on this branch at `81b215b`, before any of this plan landed:
**2927 passed in 131s.** If your baseline is red, or materially below that
count, stop and report rather than starting against it — a suite that was
already broken will make every task in this plan look like it broke something.

### The worker does not hot-reload

Every backend change in this plan is in the worker's path. After each one:

```bash
docker compose restart worker      # main stack
# or, for the worktree stack:
./ops/worktree-up.sh               # then restart its worker
```

Skip this and the job runs the old in-memory code while appearing to run the
fix.

### Manual verification needs two real jobs

Several tasks end at a browser, and two specific jobs exercise the two halves
of this change:

- **A Flye assembly** — phase-only, `pct` genuinely unknown. This is the job
  that currently shows a bar stuck at 0%.
- **A fastp trim** — reports a real percentage, so it exercises the ETA
  extrapolation.

From this worktree: `./ops/worktree-up.sh` (UI on 5273, API on 8100). Do not
use plain `docker compose` from a worktree — a `PreToolUse` hook blocks it,
because it would silently repoint the 5173 stack at this branch.

---

## Task 1: `pct` can be unknown

**Files:**
- Modify: `backend/app/models/job.py`
- Test: `backend/tests/queue/test_progress_throttle.py`

**This is the bug the rest of the plan is built on.** `JobProgress.pct` is
`float = 0.0`, and `JobContext.progress()` (`registry.py:80`) filters out
`None` values before they reach the writer. So `assembly_handlers.py:96`
passing `pct=None` does not clear anything — the field keeps the `0.0` default
for the entire run, and Flye renders behind a bar reading 0% for minutes.

The fix is one field default, and it works *because* of the `None` filter
rather than in spite of it: with the default at `None`, a handler that never
passes a number leaves it `None`, which is now the honest value. No sentinel
is needed.

- [ ] **Step 1: Write the failing test**

```python
async def test_phase_only_job_reports_unknown_pct(...):
    """Flye, Clair3 and minimap2 cannot produce an honest fraction. The model
    has to be able to say so -- a bar at 0% for a six-minute run is a lie the
    UI cannot distinguish from a stalled job."""
    # A job whose handler only ever reports phases must end with progress.pct
    # None, not 0.0.
```

- [ ] **Step 2: Change the default**

```python
class JobProgress(BaseModel):
    # None means indeterminate, not zero: a tool that cannot produce an honest
    # fraction (Flye, Clair3, minimap2 -- see assembly_runner.py:83) reports
    # phases only, and a bar rendered at 0% for its whole run is
    # indistinguishable from a stalled job.
    pct: float | None = None
```

- [ ] **Step 3: Full suite**

`pct` is read in several places; this is a type change on a hot field. Run the
whole suite, not just the queue tests, and read the count.

**Known limitation, do not fix here:** a handler that sets `pct=0.5` and later
wants to return to "unknown" cannot, because `None` means "unchanged" on the
way in. No handler does this today. Adding a sentinel to support a case with
no caller is not worth the API surface.

---

## Task 2: Generic units alongside bytes

**Files:**
- Modify: `backend/app/models/job.py`, `backend/app/queue/registry.py`
- Test: `backend/tests/queue/test_progress_throttle.py`

`bytes_done`/`bytes_total` stay untouched — they are used by hashing and chunk
assembly, and bytes render as a human-readable size rather than "3 of 7".
The new triple covers what bytes cannot: chunks, reads, contigs, records.

- [ ] **Step 1: Write the failing test** — a handler reporting
      `units_done=3, units_total=7, unit_label="chunks"` round-trips through
      `ctx.progress()`, the throttled write, and a read of the job document.

- [ ] **Step 2: Add the fields** to `JobProgress` and the corresponding
      keyword arguments to `JobContext.progress()`, following the existing
      `None`-filtering pattern exactly.

- [ ] **Step 3:** `./backend/run-worktree-tests.sh tests/queue -q`

No handler is required to populate these in this task. They exist so the
instrumentation slices under epic #6 have somewhere to put "N of M".

---

## Task 3: Live resource observations ride the progress path

**Files:**
- Modify: `backend/app/models/job.py`, `backend/app/queue/executor.py`
- Test: `backend/tests/queue/test_progress_throttle.py`, `backend/tests/queue/test_resource_sampler.py`

**The interesting part of this task is who triggers the write.**

`ResourceSampler` already polls the job's subtree once a second
(`executor.py:_sample_resources`) and already tracks running peaks. Every
reading is discarded except the final peaks, which go to `job_timings` on
completion.

The naive implementation — merge the sampler's current readings into
`_write_progress` — silently does nothing for exactly the jobs that need it
most. A phase-only Flye run calls `ctx.progress()` a handful of times in six
minutes, so there are almost no ticks to merge into. The sampler loop has to
*drive* a tick itself.

- [ ] **Step 1: Write the failing test**

```python
async def test_resources_reach_the_job_without_handler_progress_calls(...):
    """The regression that a merge-only implementation passes and the app
    fails. A phase-only job makes almost no progress calls, so resources
    merged into handler-driven ticks would never be written for the exact
    jobs a user most wants to watch."""
    # Run the sampler loop against a job whose handler reports nothing.
    # Assert rss_bytes lands on the job document anyway.
```

Write this first and watch a merge-only implementation fail it.

- [ ] **Step 2: Add the fields**

```python
    # Current and running-peak, both nullable. Current says what the machine
    # is doing now; peak says whether the job already touched the ceiling,
    # which is the question asked after an unexplained failure.
    rss_bytes: int | None = None
    cpu_percent: float | None = None
    peak_rss_bytes: int | None = None
    peak_cpu_percent: float | None = None
```

- [ ] **Step 3: Hold the sampler per job, and let it tick**

`_start_sampler` needs the job id, epoch and owner so its loop can call
`_schedule_progress`. Keep the sampler in a per-job dict alongside
`_last_progress`/`_last_phase`, and **clean it up in the same `finally`
block** — those two are popped at `executor.py:157`, and a sampler left behind
holds `psutil.Process` objects for a dead tree.

After each `observe()`, schedule a progress update carrying the four resource
fields. The existing 0.5s throttle applies unchanged, so a 1 Hz sampler
produces at most 1 Hz of writes.

- [ ] **Step 4:** the `RESOURCE_FLOOR_MS` (60s) floor **does not apply here.**

That floor exists because `job_timings` feeds predictive models and a peak from
a handful of samples is a bad input to a fit. A number displayed live is not an
input to anything. Leave `_record_timing`'s use of the floor exactly as it is —
the two consumers want different things from one sampler, and a future reader
will be tempted to "fix" the inconsistency. Say so in a comment.

- [ ] **Step 5:** full suite.

---

## Task 4: Run membership on the event

**Files:**
- Modify: `backend/app/queue/registry.py`, `backend/app/queue/queue.py`, `backend/app/queue/executor.py`
- Test: `backend/tests/queue/test_event_channels.py`

**`run_ids` is a list, and this is not a style preference.**
`models/run.py:187` records that a scalar `run_id` on `Job` was explicitly
rejected: `build_index` is deduplicated by content, so a second alignment
against the same reference reuses the first one's build and that job belongs
to two runs. `RunJob` is a link collection for that reason. A scalar field
here recreates the bug somewhere it will be found much later.

- [ ] **Step 1: Write the failing test** — a job linked to two runs publishes a
      `job.progress` event carrying **both** ids. This is the test a scalar
      implementation passes with one of them silently dropped, so assert the
      set, not the length.

- [ ] **Step 2:** add `run_ids: list[str] = field(default_factory=list)` to
      `JobContext`, resolved in `mark_running` via the existing `by_job` index
      on `run_jobs`, and carried into the published event by `_write_progress`.

Resolve **once per attempt**, never per tick. `registry.py:39` explains the
rule for `owner` and it applies verbatim: the throttled writer runs twice a
second per job and must not read documents to answer questions whose answers
cannot change mid-run.

- [ ] **Step 3:** `./backend/run-worktree-tests.sh tests/queue -q`

Nothing consumes `run_ids` yet. It ships now because the plumbing *is* the
whole change, and doing it later means reopening the same path with a live
consumer attached.

---

## Task 5: Reset on requeue, keep the high-water mark

**Files:**
- Modify: `backend/app/models/job.py`, `backend/app/queue/queue.py`, `backend/app/api/v1/jobs.py`
- Test: `backend/tests/queue/test_lifecycle.py`

Two cases behave differently, and **only one is broken**:

- *Terminal failure* already does the right thing — nothing clears progress, so
  a failed job sits at 80% next to its error, which is the most useful thing it
  could show. **Change nothing here.**
- *Requeue and retry* is broken. Nothing in `queue.py` resets progress on a
  lease expiry or a backoff, so a job that died at 80% comes back showing 80%
  while it restarts from zero.

- [ ] **Step 1: Write the failing tests**

```python
async def test_requeue_clears_progress_and_keeps_the_mark(...):
    """A job that died at 80% must not come back claiming 80%. What it should
    say instead is 'attempt 2; attempt 1 reached 80% at assembly, peaking at
    14.2 GB' -- the shape of a job hitting the same OOM every time."""

async def test_first_attempt_stashes_nothing(...):
    """No previous attempt, no stash. The field stays None rather than
    holding an empty record that the UI would have to special-case."""

async def test_terminal_failure_keeps_its_progress(...):
    """Guards the half that is already correct."""
```

- [ ] **Step 2:** add an `AttemptProgress` model (`attempt`, `pct`, `phase`,
      `message`, `peak_rss_bytes`) and `last_attempt_progress:
      AttemptProgress | None` on `Job`.

On `Job`, not inside `JobProgress` — `JobProgress` describes the current
attempt, and nesting the previous one inside it invites code that reads a
percentage without noticing which attempt it belongs to.

- [ ] **Step 3:** stash-then-clear in `mark_running`, which already writes
      `timing.started_at` once per attempt. Only the previous attempt is kept:
      the interesting comparison is with the last one, and an unbounded array
      on a hot document to answer a rarer question is not the trade to make.

- [ ] **Step 4:** surface `last_attempt_progress` on `JobOut`.

- [ ] **Step 5:** full suite.

---

## Task 6: `eta_seconds`, derived and never stored

**Files:**
- Modify: `backend/app/services/timing_service.py`, `backend/app/queue/registry.py`, `backend/app/queue/queue.py`, `backend/app/queue/executor.py`, `backend/app/api/v1/jobs.py`
- Test: `backend/tests/services/test_timing_service.py`, `backend/tests/api/` (job detail)

Two estimators, and the design work is choosing between them per tick:

1. `timing_service.estimate(job_type, input_bytes)` — available at t=0, blind
   to how this run is actually going. Already served on `GET /jobs/{id}` as
   `timing_estimate`.
2. `elapsed / pct` — needs a real `pct`, self-corrects as the run proceeds.

Prefer (2) above a `pct` floor of **0.05**, fall back to (1), null when neither
applies.

- [ ] **Step 1: Write the failing tests** — one per branch, and specifically
      one asserting that `pct=0.01` does *not* extrapolate. At one percent the
      extrapolation multiplies elapsed time by a hundred, and the first percent
      of a job is its least representative stretch (process startup, index
      loading). That test is the floor's reason for existing.

- [ ] **Step 2:** a pure helper — `eta_seconds(pct, elapsed_s, model_ms)` —
      taking numbers, not documents, so every branch is testable without a
      database.

- [ ] **Step 3:** cache the model's estimate on `JobContext` at
      `mark_running`, the same way and for the same reason as `run_ids`. The
      prediction is a function of job type and input size, both fixed at claim
      time, so it cannot change during the run. With it cached, both the SSE
      path and the API route derive an ETA with **no reads at all**.

- [ ] **Step 4:** derive at emit time in `_write_progress`, and at read time in
      `get_job`. **Never persist it** — a stored ETA is wrong by exactly the
      time since it was stored.

- [ ] **Step 5:** leave `timing_estimate` and `memory_estimate` on the detail
      route alone. `eta_seconds` is not their replacement: they describe what
      runs of this type usually cost and carry their own confidence
      (`samples`, `r_squared`), while `eta_seconds` is one number about this
      run.

- [ ] **Step 6:** full suite.

---

## Task 7: Optional phase structure

**Files:**
- Modify: `backend/app/models/job.py`, `backend/app/queue/registry.py`, `backend/app/pipelines/align_runner.py`, `backend/app/pipelines/fastp_runner.py`
- Test: `backend/tests/queue/test_pipeline_handlers.py`

`phase_index: int | None`, `phase_total: int | None`, both optional. `phase`
stays a free string.

Which runners can supply them, checked rather than assumed:

- **Can:** `fastp_runner` (`_PHASES`, a fixed ordered list), `variant_runner`
  (`_PHASE_PATTERNS`), `align_runner` (aligning → sorting), and every handler
  with hardcoded `ctx.progress(phase=...)` calls.
- **Cannot:** `assembly_runner.py:97` — and only that one.
  `_STAGE_LABELS.get(stage, stage)` deliberately displays an unrecognized Flye
  stage raw rather than leaving the phase stuck on a stale value. Flye's stage
  list is not closed, so there is no honest `phase_total`.

- [ ] **Step 1:** add the fields and the `ctx.progress()` keywords.

- [ ] **Step 2:** populate them in `align_runner` and `fastp_runner` only —
      two runners as proof the shape works. Do **not** sweep every handler in
      this task; that is churn without a consumer.

- [ ] **Step 3:** null means "unstructured — render the phase name alone."
      That is the correct representation for a genuinely open stage list, not
      a placeholder.

- [ ] **Step 4:** open a follow-up issue for assembly/Flye — whether a declared
      prefix of known stages with unknowns appended is worth it. Narrow by
      construction: one runner. Link it to epic #6.

- [ ] **Step 5:** full suite.

---

## Task 8: API and event surface

**Files:**
- Modify: `backend/app/api/v1/jobs.py`, `frontend/src/api/types.ts`
- Test: `backend/tests/api/`

No new endpoints. The two paths that already exist carry the widened model.

- [ ] **Step 1:** `progress` on `JobOut` is a plain `dict` from
      `model_dump(mode="json")`, so the new fields flow through with no change
      — **verify that with a test rather than assuming it**, since the point of
      the task is that the wire format actually changed.

- [ ] **Step 2:** update `JobSummary["progress"]` in `frontend/src/api/types.ts`
      to match, `pct: number | null` included. The `null` is what makes
      TypeScript find the render sites in Task 9 for you.

- [ ] **Step 3:** full suite.

---

## Task 9: The UI stops lying about zero

**Files:**
- Modify: `frontend/src/components/JobList.tsx`, `frontend/src/components/QueuePanel.tsx`, `frontend/src/components/ActivePipelineJobs.tsx`, `frontend/src/components/activity/ActivityLead.tsx`

Making `pct` nullable in the API type turns each of these into a type error,
which is the point. Every one currently coerces with `?? 0`:

- `JobList.tsx:52` and `:99` — bar width from `pct`.
- `QueuePanel.tsx:79`, `:93`, `:95` — already halfway there, dimming the track
  when `pct` is falsy and falling back to `width: 100%`. That accidental
  almost-indeterminate rendering is the behaviour to make deliberate.
- `ActivePipelineJobs.tsx:60` — already hides "0%", so it degrades gracefully.
- `ActivityLead.tsx:175` — run-level, derived from `done / jobs.length` rather
  than from `pct`. Leave the derivation alone; only its per-job label at :175
  is affected.

- [ ] **Step 1:** null renders an indeterminate bar (an animated or full-width
      dimmed track), never a bar at zero. A determinate zero — a job that
      genuinely reports 0% — keeps rendering as an empty bar. **These are
      different states and must look different**, which is the entire user-facing
      point of Task 1.

- [ ] **Step 2:** show live RSS beside a running job's phase. Keep it to one
      number; the full resource display is not this slice.

- [ ] **Step 3:** show `last_attempt_progress` when `attempts > 1`. `JobList.tsx`
      already renders an "attempt 2/5" line — extend that, do not add a new one.

- [ ] **Step 4: verify in a browser.** There is no headless component testing in
      this repo and none is expected (`CLAUDE.md`). Run a real Flye assembly and
      a real fastp trim on the worktree stack and watch the Activity tab:
      - Flye: indeterminate bar, phase name, RSS that moves.
      - fastp: a percentage, and an ETA that converges rather than jumping.

      Both are things a fixture-fed test reports as working while the app shows
      a bar stuck at zero — which is exactly how the current bug survived.

---

## Task 10: Close out

- [ ] **Step 1:** full suite, and **read the count**, not the exit code.

- [ ] **Step 2:** `docs/TODO.md`'s "Observability in tools: progress reporting
      and resource transparency" entry **stays open**. This slice resolves the
      model, the transport, and the persistence question; the per-tool
      instrumentation the entry also asks for does not ship here. Per
      `CLAUDE.md`, a partially-resolved entry stays in `docs/TODO.md` rather
      than moving — moving it would bury the still-open part. Add a dated note
      recording what landed, and specifically that **the entry's proposed
      separate observability container with its own pub/sub broker was
      rejected**: Redis pub/sub plus the job document already is that broker,
      and the sketch predates the current progress path. That delta is the most
      valuable sentence in the entry.

- [ ] **Step 3:** commit in separable pieces. The model widening, the executor
      change, and the frontend render are three reviewable commits; one
      squashed commit is one nobody can revert.

- [ ] **Step 4:** merge to `main` and push to `origin` once green — no
      approval needed (`CLAUDE.md`). Confirm `main` is clean first, and re-run
      the suite if it moved under you.

- [ ] **Step 5:** update [#24](https://github.com/syntheticgio/bioflow/issues/24)
      — close it, and note on [epic #6](https://github.com/syntheticgio/bioflow/issues/6)
      which of its acceptance criteria this slice satisfied and which remain
      (tool instrumentation). Mention `run_ids` on
      [#18](https://github.com/syntheticgio/bioflow/issues/18) so its
      aggregation work knows the primitive exists.
