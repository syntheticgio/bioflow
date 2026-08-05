# Per-object computation provenance — implementation plan

**Date:** 2026-08-05

**Issue:** [#9](https://github.com/syntheticgio/bioflow/issues/9)

**Spec:** [`docs/superpowers/specs/2026-08-05-object-provenance-panel-design.md`](../specs/2026-08-05-object-provenance-panel-design.md)

The spec records no open questions. This plan turns it into an ordered build.
Where this plan makes a call the spec did not, it is marked **[plan decision]**.

## Shape of the change

Five phases, each independently committable and revertable.

Phase 1 is the two executor defects. It ships **first**, ahead of the feature,
which reverses the ordering the paired-read plan used. The reason is specific
to this issue: without it there is no row in the database with a `tool` or a
`threads` value, so every later phase would be built and verified against
columns that are null by construction. Landing it first means Phase 5's manual
check can run a real job and see a populated row -- the only end-to-end
evidence available that the write path and the read path agree.

Phases 2--3 are the backend read surface. Phase 4 is the UI. Phase 5 verifies
against the running app and closes the backlog entry.

The executor defects and the read surface are genuinely unrelated changes that
happen to share a feature. Keeping them in separate commits is what makes a
regression in either attributable.

## Phase 1 — executor: `tool`, `tool_version`, `threads`

**Files:** `backend/app/queue/executor.py`,
`backend/tests/queue/test_record_outcomes.py`

Three arguments added to the `timing_service.record()` call at
`executor.py:348`. Nothing else in `_record_timing` changes.

**`threads`.** Replace `job.payload.get("threads")` with a nested read first
and the flat key as fallback:

```python
params = job.payload.get("params") or {}
threads = params.get("threads") or job.payload.get("threads")
```

`params` is defensive against `None`, not just a missing key -- a payload
carrying `"params": null` is not something to discover in a `finally` block.

**`tool`.** A module-level helper, not a per-job-type mapping:

```python
_TOOL_KEYS = ("tool", "aligner", "assembler")


def _tool_from_payload(payload: dict) -> str | None:
    """Whichever key names the binary this job ran.

    Deliberately not a {job_type: key} mapping. That shape skips a job type
    nobody added an entry for, silently, which is the failure CLAUDE.md's
    "hand-maintained registries" section describes. Reading whichever key is
    present degrades to None for job types that name no tool -- the honest
    answer for ingest_headers.
    """
```

Verified against real payloads: `trim_reads` carries `tool: "fastp"`,
`align_reads` and `build_index` carry `aligner: "star"`, `run_qc` and
`assemble_upload` carry neither.

**[plan decision]** `_TOOL_KEYS` is ordered, and the order is load-bearing:
`tool` wins over `aligner` if a payload ever carries both. Nothing does today.
The tuple is checked in order rather than by a dict lookup so the precedence is
visible at the definition.

**`tool_version`.** Cache-read only, per the spec:

```python
tool_version = None
if tool:
    seeded = tools._seeded.get(tool)
    tool_version = seeded[1].version if seeded else None
```

**[plan decision]** Reaching into `tools._seeded` from the executor is wrong at
the module boundary. Add `tools.cached_version(name: str) -> str | None`
instead, which returns the seeded version without probing and without the
fingerprint re-validation `_probe` does. Two comments earn their place there:
that a stale-fingerprint entry is acceptable for a *record of what ran* in a
way it is not for a capability check, and that this function must never grow a
probe fallback, because its caller is in the executor's `finally` and a
12-second NanoPlot probe there delays every job's completion.

If a reviewer disagrees with serving a possibly-stale version, the fallback is
to re-validate the fingerprint and return `None` on a mismatch -- still no
subprocess. Do not resolve this by probing.

**Tests** (`test_record_outcomes.py` already covers this collection):

- `payload={"params": {"threads": 8}}` records `threads == 8`.
- `payload={"threads": 8}` records `threads == 8` (the fallback).
- `payload={"params": None}` records `threads is None` and does not raise.
- `trim_reads`-shaped payload records `tool == "fastp"`.
- `align_reads`-shaped payload records `tool == "star"`.
- `ingest_headers`-shaped payload records `tool is None`.
- With nothing seeded, `tool_version is None` and no subprocess runs. Assert
  the last part by patching `tools._probe` to raise -- a test that only checks
  the value passes whether or not a probe happened.

The first three fail against today's code. That is the point; run them red
before fixing.

## Phase 2 — `records_for_object` grows a limit

**Files:** `backend/app/services/timing_service.py`,
`backend/tests/queue/test_record_outcomes.py`

```python
async def records_for_object(object_id: str, *, limit: int | None = None) -> list[JobRunTiming]:
```

Unbounded stays the default so existing callers and the test suite are
unaffected. The route passes `limit + 1` and truncates, which is where
`has_more` comes from.

Keep the existing docstring's explanation of why this reader includes failures
while `_samples` does not -- that paragraph is the whole reason the function is
named the way it is, and it must not be lost to a signature edit.

One test: `limit=2` against three inserted records returns the two newest.

## Phase 3 — the route

**Files:** `backend/app/api/v1/schemas.py`, `backend/app/api/v1/objects.py`,
`backend/tests/api/test_object_computations.py` (new)

### Schema

`ComputationRecord` in `schemas.py`, with a `def of(cls, t: JobRunTiming)`
classmethod like every other `*Out` model in that file. Fields exactly as the
spec's table lists them. `machine` flattens to `cpu_model`, `logical_cores`,
`total_ram_bytes`, `platform` -- `machine_id` is dropped deliberately, and the
model carries a one-line comment saying so, because "the field exists upstream
and is missing here" otherwise reads as an oversight.

`ObjectComputationsOut`:

```python
class ObjectComputationsOut(BaseModel):
    produced_by: ComputationRecord | None
    produced_by_job: str | None
    records: list[ComputationRecord]
    has_more: bool
```

`produced_by_job` is on the response even when `produced_by` resolves. It is
what lets the UI tell "nothing ever ran" from "the run that made this predates
2026-08-03", and that distinction is the tab's default state, not an edge case.

### Route

```python
@router.get("/{object_id}/computations", response_model=ObjectComputationsOut)
async def object_computations(
    object_id: PydanticObjectId,
    owner: OwnerDep,
    limit: int = Query(100, le=500),
) -> ObjectComputationsOut:
```

Body, in order:

1. `obj = await object_service.get_object(object_id, owner=owner)` — **[plan
   decision]** `get_object`, not `object_with_blob`. The spec named
   `object_with_blob` because that is what `get_object` (the route) uses, but
   this route never touches the blob and `object_with_blob` is a second query
   for a document it would discard. `get_object` is the same choke point and
   raises the same `NotFoundError` for a wrong owner.
2. `rows = await timing_service.records_for_object(str(object_id), limit=limit + 1)`
3. `has_more = len(rows) > limit`, then truncate to `limit`.
4. If `obj.produced_by_job`, find the `JobRunTiming` with that `job_id`.

Step 1 must stay first and must not be reordered into a `gather` with step 2.
`JobRunTiming` has no owner field, so the object fetch *is* the authorization;
running the record query concurrently reads another profile's rows before the
check that says it may not, even if the response is later discarded.

The `produced_by` lookup is a direct `JobRunTiming.find_one(job_id == ...)`.
Note for whoever implements it: `job_id` has **no index** (`timing.py`'s
`Settings.indexes` has `model_samples` and `by_object` only). At 111 rows a
collection scan is free. **[plan decision]** No index in this phase; add one
only if the collection grows enough to matter, and note it in the follow-ups
rather than adding an index on speculation.

### Client method

`getObjectComputations: (id: string) => request<ObjectComputations>(...)` in
`frontend/src/api/client.ts` beside `getObject` (line 231), with the types in
`frontend/src/api/types.ts`.

### Tests — `backend/tests/api/test_object_computations.py`

Follow `tests/api/conftest.py`'s `client` and `two_profiles` fixtures. This is
a real-database test module, not a `bare_app` one: it needs `DataObject` rows
with a real `produced_by_job` and real `JobRunTiming` documents.

- Records come back newest-`finished_at` first.
- **A `failed` record is present in the response.** This is the assertion the
  feature exists for -- every other reader of this collection filters failures
  out, and a regression that quietly reused `_modelled()` would pass every
  other test here.
- Profile B gets a 404 for profile A's object. Assert the negative direction;
  A-reads-A's-own passes whether or not scoping works.
- `produced_by` resolves from `produced_by_job` to the matching row.
- `produced_by` is `null` while `produced_by_job` is populated, when no timing
  row carries that `job_id`. This is the common real case, not a contrived one.
- `has_more` is `true` at `limit + 1` records, `false` at exactly `limit`.
- A record with `resources.peak_rss_bytes = None` serializes as `null`, not
  `0`. A test that only checks the happy path lets a `or 0` coercion through,
  and `0 B` of peak RSS is a measurement rather than the absence of one.

## Phase 4 — the History tab

**Files:** `frontend/src/components/ComputationHistory.tsx` (new),
`frontend/src/components/DetailPanel.tsx`

### Wiring

`tabsFor` (`DetailPanel.tsx:328`) gains `{ id: "history", label: "History" }`,
placed after `metadata` and before `actions`. Unconditional -- every object can
have computations run on it, and a tab that appears only once a record exists
is a tab nobody learns is there.

**[plan decision] No hint on the tab.** The spec suggested a record count.
The count lives in the query, the query lives in the panel, and the tab strip
renders in `DetailPanel` before any panel mounts -- so a hint means fetching
provenance on every object selection to label a tab whose answer is almost
always zero. The hint is the wrong trade here; the other tabs' hints come from
data `DetailPanel` already holds.

Render site alongside the others (`DetailPanel.tsx:780`-ish):

```tsx
{tab === "history" && (
  <TabPanel id="history" idPrefix="obj">
    <ComputationHistory obj={obj} />
  </TabPanel>
)}
```

The tab id goes in the URL like the rest (`?tab=history`), which the existing
`setTab` handles with no change.

### The panel

`useQuery({ queryKey: ["object-computations", obj.id], queryFn: ... })`,
matching the file's existing patterns. No `refetchInterval` — a record is
written once, when a job ends, so there is nothing to poll for.

Freshness comes from the SSE stream instead. `hooks/useEvents.ts` already
debounces job events into invalidation keys (`schedule("jobs")` at line 89);
add a `computations` key scheduled on the terminal job events only — not
`job.progress`, which fires several times a second and cannot have produced a
record.

**There is a race here, and it is worth knowing about rather than designing
around.** `queue.complete()` publishes `job.succeeded` from the executor's
`try` block (`executor.py:145`), while `_record_timing` runs afterwards in the
`finally` (`executor.py:208`). The event therefore precedes the record. The
500 ms debounce in `useEvents` covers it in practice, and the realistic path —
a user opening the History tab some time after the job finished — refetches on
mount regardless. **[plan decision]** Do not reorder the executor to close
this. Recording is telemetry wrapped in a bare `except` precisely so it cannot
affect job completion, and moving it ahead of `complete()` to tidy a UI refresh
would put it back on the critical path.

Structure:

- **How this file was made** — the `produced_by` record as a labelled block of
  key/value pairs, not a one-row table. Omitted entirely when null.
- **Runs on this file** — `<table className="trim-table">`, the class
  `ContigTable` and `VariantTable` already use. Columns: when, job type, tool
  and version, duration, threads, peak RSS, machine, outcome.
- Outcome as a badge; `failed` and `dead` visually distinct from `succeeded`,
  `cancelled` neutral. A failed run is what this panel is for.
- **Every null renders as an em-dash.** Never `0`, never blank. A run under the
  60-second sampling floor has no RSS measurement, and `0 B` claims one. This
  applies to threads, tool, version, RSS and machine alike.
- Job type renders `className="mono"`, as `JobList.tsx:65` does.

### The three states

Loading and error follow `CompletenessDialog.tsx:34`, which already destructures
`isLoading`/`isError`/`error` from `useQuery` — do not invent a third
convention.

Empty is the one that needs writing, because it is what nearly every object
shows:

- `records` empty **and** `produced_by_job` null → "No computations have been
  recorded for this file."
- `records` empty **and** `produced_by_job` set → copy that does **not** say
  nothing ran, because something demonstrably did. Say that computation records
  began on 2026-08-03 and earlier runs were not recorded.

Write the second string carefully. It is the message on 33 of the 49 objects in
the real database, and the version of it that says "no computations" is a
factual claim the data contradicts.

## Phase 5 — verify against the running app

Worktree stack, per CLAUDE.md:

```bash
./ops/worktree-up.sh          # UI on 5273, API on 8100
```

Check, in this order:

1. A file with no records and no `produced_by_job` — the plain empty state.
2. Object `6a6f64490d673b9d20bbeeab` — the one row in the real database with an
   `object_id`, a *failed* 30 ms `ingest_headers`. The failure must render, and
   render as a failure.
3. A derived BAM whose `produced_by_job` predates 2026-08-03 — `produced_by` is
   null, and the copy must not claim nothing produced it.
4. **Run a fresh QC job from the worktree UI and watch a row appear** carrying a
   tool name, a thread count and a machine. This is the only check that proves
   Phase 1 and Phase 3 agree; every test above stubs one side or the other.

Also run the real-database check the spec asks for, from the **main checkout
root** (not the worktree — `docker compose exec api` there tests main's code):

```bash
docker compose exec api python -c "..."
```

## Test gate

From the worktree, never `docker compose exec api`:

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Read the count, not the exit code. Take the baseline by running this once on
the unchanged tree before Phase 1 rather than trusting the 1872 figure in
CLAUDE.md, which was measured some commits ago. This plan adds roughly 15.

There is no frontend test setup in this repo and none is expected — Phase 4 is
verified in the browser, which is what Phase 5 is.

## Closing out

- `docs/TODO.md` — "No provenance panel for computation records" gets
  ` — FIXED`, a note on what shipped, and **moves in full to
  `docs/TODO-done.md`**. Say what the implementation did differently: the entry
  predicted a route in `jobs.py`, and it landed in `objects.py`; the entry did
  not know the write path was blanking three of the six columns it named, nor
  that `produced_by_job` made produced-by a free lookup.
- Issue #9: `status: implementation plan` → `status:ready` when this plan
  lands; comment with what shipped and close when the work merges.
- Merge to `main` and push to `origin` once the suite is green and `main` is
  clean.

## Follow-ups (do not do these here)

- Jobs with no known input size are never recorded (`if not size: return` in
  `_record_timing`). Touches what the duration model fits; needs its own
  decision.
- An index on `job_timings.job_id` if the collection outgrows a scan.
- Tool version when the probe cache misses.
- Sanitized `params` have no surface anywhere.
- The project-wide computation view — the second of the design's three read
  surfaces.
