# Metrics Per-Run Tables Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the Metrics page a two-column layout whose right side lists each job type's 5 most recent runs, and stop the shared splash panel from rendering beside it.

**Architecture:** One new backend accessor (`runs_for_type`) and one endpoint (`GET /jobs/metrics/runs`) expose individual `job_timings` rows, which the existing `GET /jobs/metrics` never did — it returns only per-type aggregates. The frontend adds `/metrics` to `App.tsx`'s `singleColumn` list to unmount the splash `DetailPanel`, splits `Metrics.tsx` into two columns, and adds a `/metrics/:jobType` route as the see-more destination.

**Tech Stack:** FastAPI + Beanie/Motor (backend), React + TanStack Query + react-router (frontend), pytest.

Spec: `docs/superpowers/specs/2026-08-10-metrics-per-run-tables-design.md`. Issue: [#129](https://github.com/syntheticgio/bioflow/issues/129).

---

## Critical context

**Run tests from the worktree with `./backend/run-worktree-tests.sh`, never `docker compose exec api`.** The `api` container bind-mounts the *main* checkout, so `exec` silently tests main's code and reports results describing the wrong tree. See CLAUDE.md, "Verifying changes".

**The new accessor must not route through `_modelled()`.** That function filters to successful runs so failures cannot bias the predictive fits. The per-run tables must show failures. `records_for_object()` is the existing precedent for an explicitly-named opt-out accessor; follow it. This is the single mistake most likely to be made here, and Task 1's test is what catches it.

## File structure

| File | Responsibility |
|---|---|
| `backend/app/services/timing_service.py` | Add `runs_for_type()` accessor + `recent_runs_by_type()` aggregator |
| `backend/app/api/v1/jobs.py` | Add `GET /metrics/runs` route |
| `backend/tests/services/test_metrics.py` | Extend with per-run accessor tests |
| `frontend/src/api/types.ts` | Add `JobRun`, `RecentRuns` types |
| `frontend/src/api/client.ts` | Add `metricsRuns()` |
| `frontend/src/components/Metrics.tsx` | Two-column layout; extract `RunTable` |
| `frontend/src/components/MetricsJobType.tsx` | New: `/metrics/:jobType` paged page |
| `frontend/src/App.tsx` | Add routes to `singleColumn`; register `/metrics/:jobType` |
| `frontend/src/styles.css` | Add `.metrics-page` layout |

---

### Task 1: `runs_for_type()` accessor

**Files:**
- Modify: `backend/app/services/timing_service.py` (after `records_for_object`, ~line 232)
- Test: `backend/tests/services/test_metrics.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/services/test_metrics.py`:

```python
class TestRunsForType:
    """Per-run rows for the Metrics page's right column.

    The counterpart to `_modelled`, and deliberately not built on it: these
    rows are what a user reads to see what actually happened, and a failed
    run is the most informative row on the page. The first test is the one
    that fails if someone later rewires this through the outcome filter.
    """

    async def test_includes_failures(self):
        await _record(duration_ms=100_000)
        await _record(outcome=RunOutcome.FAILED, duration_ms=500)

        runs = await timing_service.runs_for_type("align_reads")
        assert {r.outcome for r in runs} == {"succeeded", "failed"}

    async def test_most_recent_first(self):
        from datetime import datetime, timezone

        for day in (1, 3, 2):
            await _record(
                finished_at=datetime(2026, 8, day, tzinfo=timezone.utc)
            )

        runs = await timing_service.runs_for_type("align_reads")
        days = [r.finished_at.day for r in runs]
        assert days == [3, 2, 1]

    async def test_limit_and_offset_page(self):
        from datetime import datetime, timezone

        for day in range(1, 6):
            await _record(
                finished_at=datetime(2026, 8, day, tzinfo=timezone.utc)
            )

        page = await timing_service.runs_for_type(
            "align_reads", limit=2, offset=2
        )
        assert [r.finished_at.day for r in page] == [3, 2]

    async def test_unknown_type_is_empty_not_an_error(self):
        assert await timing_service.runs_for_type("no_such_type") == []

    async def test_other_types_excluded(self):
        await _record(job_type="align_reads")
        await _record(job_type="call_variants")

        runs = await timing_service.runs_for_type("call_variants")
        assert len(runs) == 1
        assert runs[0].job_type == "call_variants"
```

- [ ] **Step 2: Extend the `_record` helper to accept `finished_at`**

The existing helper (~line 87) has no `finished_at` parameter, but the ordering
and paging tests need one. Add the parameter and pass it through:

```python
async def _record(
    *,
    job_type="align_reads",
    outcome=RunOutcome.SUCCEEDED,
    duration_ms=120_000,
    input_bytes=1_000_000,
    peak_rss_bytes=None,
    tool="minimap2",
    tool_version="2.28",
    features=None,
    finished_at=None,
    threads=None,
):
    resources = RunResources()
    if peak_rss_bytes is not None:
        resources.peak_rss_bytes = peak_rss_bytes
    await JobRunTiming(
        job_type=job_type,
        input_bytes=input_bytes,
        duration_ms=duration_ms,
        outcome=outcome,
        tool=tool,
        tool_version=tool_version,
        features=features or {},
        resources=resources,
        finished_at=finished_at,
        threads=threads,
    ).insert()
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/services/test_metrics.py::TestRunsForType -v
```

Expected: FAIL — `AttributeError: module 'app.services.timing_service' has no attribute 'runs_for_type'`

- [ ] **Step 4: Implement the accessor**

Add to `backend/app/services/timing_service.py`, directly after `records_for_object`:

```python
async def runs_for_type(
    job_type: str, *, limit: int | None = None, offset: int = 0
) -> list[JobRunTiming]:
    """Recent runs of one job type, **including failures**.

    The read path behind the Metrics page's per-run tables, and the third
    explicitly-named opt-out of the outcome filter alongside
    `records_for_object`. It must not be built on `_modelled`: that filter
    exists so a failed run cannot bias a predictive fit, but a user reading
    "what has call_variants been doing" is owed the failures -- they are the
    most informative rows on the page. Naming it plainly is what keeps that a
    visible choice rather than an omission.

    Newest first, so a caller taking the first N gets the most recent N.
    """
    query = JobRunTiming.find(JobRunTiming.job_type == job_type).sort(
        "-finished_at"
    )
    if offset:
        query = query.skip(offset)
    if limit is not None:
        query = query.limit(limit)
    return await query.to_list()
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/services/test_metrics.py::TestRunsForType -v
```

Expected: PASS, 5 passed

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/timing_service.py backend/tests/services/test_metrics.py
git commit -m "feat(metrics): read individual runs per job type, failures included"
```

---

### Task 2: `recent_runs_by_type()` aggregator

Serves every job type in one call, so a page rendering N tables makes one request instead of N.

**Files:**
- Modify: `backend/app/services/timing_service.py`
- Test: `backend/tests/services/test_metrics.py`

- [ ] **Step 1: Write the failing tests**

```python
class TestRecentRunsByType:
    async def test_caps_each_type_at_the_limit(self):
        for _ in range(7):
            await _record(job_type="align_reads")
        for _ in range(2):
            await _record(job_type="call_variants")

        by_type = await timing_service.recent_runs_by_type(limit=5)
        assert len(by_type["align_reads"]["runs"]) == 5
        assert len(by_type["call_variants"]["runs"]) == 2

    async def test_reports_total_so_the_ui_knows_to_offer_see_more(self):
        for _ in range(7):
            await _record(job_type="align_reads")
        for _ in range(2):
            await _record(job_type="call_variants")

        by_type = await timing_service.recent_runs_by_type(limit=5)
        assert by_type["align_reads"]["total"] == 7
        assert by_type["call_variants"]["total"] == 2

    async def test_total_counts_failures_too(self):
        await _record(job_type="qc")
        await _record(job_type="qc", outcome=RunOutcome.FAILED)

        by_type = await timing_service.recent_runs_by_type(limit=5)
        assert by_type["qc"]["total"] == 2

    async def test_covers_every_type_present(self):
        await _record(job_type="align_reads")
        await _record(job_type="call_variants")
        await _record(job_type="qc")

        by_type = await timing_service.recent_runs_by_type(limit=5)
        assert set(by_type) == {"align_reads", "call_variants", "qc"}

    async def test_empty_collection_is_empty_dict(self):
        assert await timing_service.recent_runs_by_type(limit=5) == {}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/services/test_metrics.py::TestRecentRunsByType -v
```

Expected: FAIL — no attribute `recent_runs_by_type`

- [ ] **Step 3: Implement**

Add after `runs_for_type`:

```python
async def recent_runs_by_type(*, limit: int = 5) -> dict[str, dict]:
    """The most recent `limit` runs of every job type, plus each type's total.

    One call rather than one per type: the Metrics page renders a table per
    job type, and a component fetching its own rows would turn a page load
    into N requests.

    `total` counts every recorded run of the type, failures included, so the
    UI can decide whether a "see more" link is warranted without a second
    round trip. It is deliberately the full history while `runs` is only the
    recent window -- the same split `metrics()` makes between its outcome
    counts and its summaries.
    """
    out: dict[str, dict] = {}
    for job_type in sorted(await JobRunTiming.distinct("job_type")):
        out[job_type] = {
            "runs": await runs_for_type(job_type, limit=limit),
            "total": await JobRunTiming.find(
                JobRunTiming.job_type == job_type
            ).count(),
        }
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/services/test_metrics.py::TestRecentRunsByType -v
```

Expected: PASS, 5 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/timing_service.py backend/tests/services/test_metrics.py
git commit -m "feat(metrics): serve every job type's recent runs in one query"
```

---

### Task 3: `GET /jobs/metrics/runs` endpoint

**Files:**
- Modify: `backend/app/api/v1/jobs.py` (after the `/metrics` route, ~line 218)
- Test: `backend/tests/services/test_metrics.py`

Route order matters: `/metrics/runs` must be declared before the existing
`/{job_id}` catch-all (~line 246), or FastAPI matches `runs` as a job id.
Declaring it immediately after `/metrics` satisfies this.

- [ ] **Step 1: Write the failing tests**

```python
from app.api.v1.jobs import metrics_runs


class TestRunsEndpoint:
    """The serialized shape the frontend consumes.

    Kept separate from the accessor tests because the field list is a
    contract with `frontend/src/api/types.ts` -- a rename here is a silent
    breakage there, since nothing type-checks across that boundary.
    """

    async def test_serializes_the_fields_the_table_renders(self):
        await _record(
            duration_ms=90_000,
            input_bytes=2_000_000,
            peak_rss_bytes=4_000_000_000,
            threads=8,
        )

        body = await metrics_runs()
        run = body["by_type"]["align_reads"]["runs"][0]
        assert run["outcome"] == "succeeded"
        assert run["duration_ms"] == 90_000
        assert run["input_bytes"] == 2_000_000
        assert run["peak_rss_bytes"] == 4_000_000_000
        assert run["threads"] == 8
        assert run["tool"] == "minimap2"
        assert run["tool_version"] == "2.28"

    async def test_unmeasured_memory_is_null_not_zero(self):
        # The 60s sampling floor leaves peak_rss_bytes unset. Null is the
        # absence of a measurement; 0 would claim the run used no memory.
        await _record(peak_rss_bytes=None)

        body = await metrics_runs()
        assert body["by_type"]["align_reads"]["runs"][0]["peak_rss_bytes"] is None

    async def test_single_type_query_is_paged(self):
        from datetime import datetime, timezone

        for day in range(1, 6):
            await _record(
                finished_at=datetime(2026, 8, day, tzinfo=timezone.utc)
            )

        body = await metrics_runs(job_type="align_reads", limit=2, offset=0)
        assert body["job_type"] == "align_reads"
        assert body["total"] == 5
        assert len(body["runs"]) == 2

    async def test_default_caps_each_type_at_five(self):
        for _ in range(9):
            await _record()

        body = await metrics_runs()
        assert len(body["by_type"]["align_reads"]["runs"]) == 5
        assert body["by_type"]["align_reads"]["total"] == 9
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/services/test_metrics.py::TestRunsEndpoint -v
```

Expected: FAIL — `ImportError: cannot import name 'metrics_runs'`

- [ ] **Step 3: Implement the route**

Add to `backend/app/api/v1/jobs.py` immediately after the `/metrics` route:

```python
def _run_out(record) -> dict:
    """One run as the Metrics table renders it.

    Every unmeasured value stays None rather than becoming 0: memory is only
    sampled above the executor's resource floor, so most short runs have no
    peak, and a zero here would read as "used no memory" instead of "not
    measured".
    """
    return {
        "finished_at": record.finished_at.isoformat()
        if record.finished_at
        else None,
        "outcome": record.outcome,
        "duration_ms": record.duration_ms,
        "input_bytes": record.input_bytes,
        "peak_rss_bytes": record.resources.peak_rss_bytes,
        "threads": record.threads,
        "tool": record.tool,
        "tool_version": record.tool_version,
        "job_id": record.job_id,
        "object_id": record.object_id,
    }


@router.get("/metrics/runs")
async def metrics_runs(
    job_type: str | None = None,
    limit: int = 5,
    offset: int = 0,
) -> dict:
    """Individual runs for the Metrics page's per-job-type tables.

    Two shapes from one route. Without `job_type` it returns every type's
    most recent runs at once, because the page draws a table per type and
    per-table fetching would make a page load N requests. With `job_type` it
    pages one type, which is what the "see more" page reads.

    Failures are included in both. See `timing_service.runs_for_type`.
    """
    from app.services import timing_service

    if job_type:
        runs = await timing_service.runs_for_type(
            job_type, limit=limit, offset=offset
        )
        total = await JobRunTiming.find(
            JobRunTiming.job_type == job_type
        ).count()
        return {
            "job_type": job_type,
            "total": total,
            "runs": [_run_out(r) for r in runs],
        }

    by_type = await timing_service.recent_runs_by_type(limit=limit)
    return {
        "by_type": {
            name: {
                "runs": [_run_out(r) for r in entry["runs"]],
                "total": entry["total"],
            }
            for name, entry in by_type.items()
        }
    }
```

Add `JobRunTiming` to the file's imports if not already present:

```python
from app.models.timing import JobRunTiming
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/services/test_metrics.py::TestRunsEndpoint -v
```

Expected: PASS, 4 passed

- [ ] **Step 5: Run the whole metrics file for regressions**

```bash
./backend/run-worktree-tests.sh tests/services/test_metrics.py -q
```

Expected: all pass — the pre-existing `TestMetrics` cases must still pass untouched.

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/v1/jobs.py backend/tests/services/test_metrics.py
git commit -m "feat(api): expose individual job runs at /jobs/metrics/runs"
```

---

### Task 4: Frontend types and client

**Files:**
- Modify: `frontend/src/api/types.ts` (after `MetricsStats`, ~line 237)
- Modify: `frontend/src/api/client.ts:523`

- [ ] **Step 1: Add the types**

Append after `MetricsStats` in `types.ts`:

```typescript
/**
 * One recorded run, for the Metrics page's per-job-type tables.
 *
 * Every measurement is nullable and means "not measured", never zero:
 * `peak_rss_bytes` is unset for runs below the executor's 60s sampling
 * floor, which is most short jobs.
 */
export interface JobRun {
  finished_at: string | null;
  outcome: string;
  duration_ms: number;
  input_bytes: number;
  peak_rss_bytes: number | null;
  threads: number | null;
  tool: string | null;
  tool_version: string | null;
  job_id: string | null;
  object_id: string | null;
}

/**
 * Recent runs grouped by job type, from GET /jobs/metrics/runs.
 *
 * `total` is the type's whole history (failures included) while `runs` is
 * only the recent window, so the UI can offer "see more" without a second
 * request.
 */
export interface RecentRuns {
  by_type: Record<string, { runs: JobRun[]; total: number }>;
}

/** One job type's runs, paged, from GET /jobs/metrics/runs?job_type=. */
export interface JobTypeRuns {
  job_type: string;
  total: number;
  runs: JobRun[];
}
```

- [ ] **Step 2: Add the client methods**

In `frontend/src/api/client.ts`, after line 523's `metrics`:

```typescript
  metricsRuns: () => request<RecentRuns>("/jobs/metrics/runs"),
  metricsRunsFor: (jobType: string, limit: number, offset: number) =>
    request<JobTypeRuns>(
      `/jobs/metrics/runs?job_type=${encodeURIComponent(jobType)}&limit=${limit}&offset=${offset}`,
    ),
```

Add `JobRun`, `RecentRuns`, `JobTypeRuns` to the type imports at the top of `client.ts`.

- [ ] **Step 3: Verify it compiles**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/api/client.ts
git commit -m "feat(frontend): type and fetch individual job runs"
```

---

### Task 5: Stop the splash panel rendering on Metrics

This is the fix for the issue's first complaint. `/metrics` is absent from the
`singleColumn` list, so `DetailPanel` mounts beside the page and — with no
`?sel=` param, always the case here — renders `EmptyDetail`, the component its
own docstring calls "BioFlow's de facto splash screen".

**Files:**
- Modify: `frontend/src/App.tsx:79-86`

- [ ] **Step 1: Add the routes to `singleColumn`**

Replace the `singleColumn` expression with:

```typescript
  const singleColumn =
    pathname === "/activity" ||
    pathname === "/shares" ||
    // The canvas needs the whole width to be usable at all -- a graph squeezed
    // beside the file list and a DetailPanel has no room to lay nodes out.
    pathname === "/workflows" ||
    // Metrics lays out its own two columns, the right one being per-job-type
    // run tables. A DetailPanel beside that is a third column, and with
    // nothing ever selected here it renders EmptyDetail -- the splash screen,
    // which is what #129 reported as marketing content on a data page.
    pathname.startsWith("/metrics") ||
    pathname.startsWith("/help/") ||
    pathname.startsWith("/settings");
```

- [ ] **Step 2: Register the see-more route**

Next to the existing `/metrics` route (line ~124):

```tsx
          <Route path="/metrics" element={<Metrics />} />
          <Route path="/metrics/:jobType" element={<MetricsJobType />} />
```

Add the import at the top of `App.tsx`:

```typescript
import { MetricsJobType } from "./components/MetricsJobType";
```

This import fails until Task 6 creates the file — expected; the two tasks
commit together at the end of Task 6.

- [ ] **Step 3: Commit is deferred**

`MetricsJobType` does not exist yet, so the build is red between here and the
end of Task 6. Do not commit at this step.

---

### Task 6: The `RunTable` component and the two-column page

**Files:**
- Modify: `frontend/src/components/Metrics.tsx`
- Create: `frontend/src/components/MetricsJobType.tsx`

- [ ] **Step 1: Add shared run-row helpers to `Metrics.tsx`**

Add after the existing `toolName` helper:

```tsx
/** A run's outcome as a badge; failures must read as failures at a glance. */
function OutcomeBadge({ outcome }: { outcome: string }) {
  return (
    <span className={`run-outcome run-outcome-${outcome}`}>{outcome}</span>
  );
}

/** A run's tool as "name version", or the dash when unrecorded. */
function runTool(run: JobRun): string {
  if (!run.tool) return DASH;
  return run.tool_version ? `${run.tool} ${run.tool_version}` : run.tool;
}

/** An optional measurement as text, or the dash. Never renders 0 for null. */
function opt<T>(v: T | null, f: (x: T) => string): string {
  return v == null ? DASH : f(v);
}

/**
 * One job type's runs, newest first.
 *
 * Failures are listed alongside successes, which makes this table
 * deliberately inconsistent with the medians in the left column -- those read
 * successful runs only, so a failure cannot make a job type look fast and
 * cheap. Both are correct for their question, and the outcome column is what
 * keeps the difference legible.
 */
export function RunTable({ runs }: { runs: JobRun[] }) {
  if (runs.length === 0) {
    return <p className="run-table-empty">No runs recorded yet.</p>;
  }
  return (
    <table className="help-table run-table">
      <thead>
        <tr>
          <th>Finished</th>
          <th>Outcome</th>
          <th>Duration</th>
          <th>Input</th>
          <th>Peak memory</th>
          <th>Tool</th>
        </tr>
      </thead>
      <tbody>
        {runs.map((run, i) => (
          <tr key={run.job_id ?? `${run.finished_at}-${i}`}>
            <td className="mono">
              {opt(run.finished_at, (s) => new Date(s).toLocaleString())}
            </td>
            <td>
              <OutcomeBadge outcome={run.outcome} />
            </td>
            <td className="mono">{formatDuration(run.duration_ms)}</td>
            <td className="mono">{formatBytes(run.input_bytes)}</td>
            <td className="mono">{opt(run.peak_rss_bytes, formatBytes)}</td>
            <td>{runTool(run)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
```

Add `JobRun` to the type imports at the top of `Metrics.tsx`.

- [ ] **Step 2: Restructure `MetricsBody` into two columns**

Replace the `return (...)` block of `MetricsBody` with:

```tsx
  return (
    <div className="metrics-page">
      <div className="metrics-overview">
        <h1>Metrics</h1>
        <p className="help-intro">
          What BioFlow's computations have cost — how long they took, how much
          memory they used, how big the inputs were — recorded from every run.
        </p>

        <section className="help-section">
          <h2>Overview</h2>
          <FileHeadlineStats stats={stats} />
          <p>
            Duration, memory, input-size and read-count numbers describe the
            most recent successful runs of each job type (at most{" "}
            {min_samples} each); run counts cover every recorded run, failures
            included. Memory is only sampled for runs of{" "}
            {formatDuration(resource_floor_ms)} or more — a shorter run has no
            peak to report, so it shows as {DASH}, not zero.
          </p>
        </section>

        <section className="help-section">
          <h2>By job type</h2>
          {/* Unchanged: the existing aggregate table, kept as the
              cross-type comparison. */}
          <table className="help-table">
            {/* ...existing thead and tbody, verbatim... */}
          </table>
        </section>
      </div>

      <div className="metrics-runs">
        <RecentRunsColumn />
      </div>
    </div>
  );
```

Keep the existing `<thead>`/`<tbody>` exactly as they are — only the wrapper
changes.

- [ ] **Step 3: Add the right column**

Add to `Metrics.tsx`:

```tsx
/**
 * The right column: one table per job type, most recent runs first.
 *
 * Every type is drawn on load rather than behind a selection, so the column
 * answers "what has each job type been doing" without a click. One request
 * serves them all -- see the endpoint's own note on why.
 */
function RecentRunsColumn() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["jobs", "metrics", "runs"],
    queryFn: api.metricsRuns,
  });

  if (isLoading) return <p className="help-intro">Loading runs…</p>;
  if (isError || !data) return <p className="help-intro">Couldn't load runs.</p>;

  const types = Object.keys(data.by_type).sort(
    (a, b) => data.by_type[b].total - data.by_type[a].total || a.localeCompare(b),
  );

  if (types.length === 0) {
    return (
      <section className="help-section">
        <h2>Recent runs</h2>
        <p className="run-table-empty">
          No runs recorded yet — this fills in as jobs complete.
        </p>
      </section>
    );
  }

  return (
    <section className="help-section">
      <h2>Recent runs</h2>
      {types.map((jobType) => {
        const entry = data.by_type[jobType];
        return (
          <div className="run-group" key={jobType}>
            <div className="run-group-head">
              <h3 className="mono">{jobType}</h3>
              {entry.total > entry.runs.length && (
                <Link className="run-see-more" to={`/metrics/${jobType}`}>
                  See all {entry.total.toLocaleString()} →
                </Link>
              )}
            </div>
            <RunTable runs={entry.runs} />
          </div>
        );
      })}
    </section>
  );
}
```

Add to the imports at the top of `Metrics.tsx`:

```typescript
import { Link } from "react-router-dom";
```

- [ ] **Step 4: Create the see-more page**

Create `frontend/src/components/MetricsJobType.tsx`:

```tsx
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery, keepPreviousData } from "@tanstack/react-query";
import { api } from "../api/client";
import { RunTable } from "./Metrics";

const PAGE = 25;

/**
 * Every recorded run of one job type, paged.
 *
 * The "see more" destination from the Metrics page. Deliberately a plain
 * table rather than a second summary: the medians already live one page
 * back, and what this page adds is the individual rows behind them --
 * failures included.
 */
export function MetricsJobType() {
  const { jobType = "" } = useParams();
  const [page, setPage] = useState(0);

  const { data, isLoading, isError } = useQuery({
    queryKey: ["jobs", "metrics", "runs", jobType, page],
    queryFn: () => api.metricsRunsFor(jobType, PAGE, page * PAGE),
    // Without this the table blanks to "Loading…" on every page step, which
    // reads as the data vanishing rather than advancing.
    placeholderData: keepPreviousData,
  });

  return (
    <div className="metrics-page metrics-page-single">
      <div className="metrics-overview">
        <p className="help-intro">
          <Link to="/metrics">← Metrics</Link>
        </p>
        <h1 className="mono">{jobType}</h1>

        {isLoading && <p className="help-intro">Loading…</p>}
        {isError && <p className="help-intro">Couldn't load runs.</p>}

        {data && (
          <section className="help-section">
            <h2>
              {data.total.toLocaleString()} recorded run
              {data.total === 1 ? "" : "s"}
            </h2>
            <RunTable runs={data.runs} />

            <div className="run-pager">
              <button
                className="btn"
                disabled={page === 0}
                onClick={() => setPage((p) => Math.max(0, p - 1))}
              >
                ← Newer
              </button>
              <span className="run-pager-at">
                {(page * PAGE + 1).toLocaleString()}–
                {Math.min((page + 1) * PAGE, data.total).toLocaleString()} of{" "}
                {data.total.toLocaleString()}
              </span>
              <button
                className="btn"
                disabled={(page + 1) * PAGE >= data.total}
                onClick={() => setPage((p) => p + 1)}
              >
                Older →
              </button>
            </div>
          </section>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 5: Verify it compiles**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors. This also clears the red build Task 5 left.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/App.tsx frontend/src/components/Metrics.tsx frontend/src/components/MetricsJobType.tsx
git commit -m "feat(metrics): list each job type's recent runs beside the summary"
```

---

### Task 7: Styling

**Files:**
- Modify: `frontend/src/styles.css` (append near the help-page block, ~line 2726)

- [ ] **Step 1: Add the layout**

```css
/* Metrics page ---------------------------------------------------------- */

/* Its own class rather than .help-page: that one is a 760px prose measure,
   and this page is two columns of data tables. Same reason .software-page
   overrides the measure. #129 -- the narrow measure is also why the page
   looked like it had a marketing column stranded on the right. */
.metrics-page {
  display: grid;
  grid-template-columns: minmax(0, 3fr) minmax(0, 2fr);
  gap: 32px;
  padding: 24px 32px;
  overflow-y: auto;
  color: var(--text);
  align-items: start;
}

/* The see-more page has no second column to balance. */
.metrics-page-single {
  grid-template-columns: minmax(0, 1fr);
  max-width: 1100px;
}

.metrics-page h1 {
  font-size: 20px;
  margin: 0 0 4px;
}

.metrics-overview,
.metrics-runs {
  min-width: 0;
}

.run-group {
  margin-bottom: 28px;
}

.run-group-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  border-bottom: 1px solid var(--border);
  padding-bottom: 4px;
}

.run-group-head h3 {
  font-size: 13px;
  margin: 0;
  color: var(--text);
}

.run-see-more {
  font-size: 12px;
  white-space: nowrap;
}

.run-table {
  font-size: 12px;
}

.run-table-empty {
  color: var(--text-secondary);
  font-size: 12px;
  margin: 8px 0;
}

.run-outcome {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.03em;
}

.run-outcome-succeeded {
  color: var(--text-dim);
}

/* Failures are listed here on purpose, so they have to read as failures
   rather than as another row. */
.run-outcome-failed,
.run-outcome-dead {
  color: var(--error);
}

.run-outcome-cancelled {
  color: var(--text-secondary);
}

.run-pager {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 16px;
}

.run-pager-at {
  font-size: 12px;
  color: var(--text-dim);
}

/* One column is unreadable at tablet width and below: six columns of run
   data cannot share a viewport with the summary table. */
@media (max-width: 1100px) {
  .metrics-page {
    grid-template-columns: minmax(0, 1fr);
  }
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/styles.css
git commit -m "style(metrics): lay the page out as summary and runs, not prose"
```

---

### Task 8: Full suite and manual verification

- [ ] **Step 1: Run the whole backend suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: green. **Read the pass/fail count, not the exit code** — CLAUDE.md
is explicit that "green" means the count. A rotating handful of DB-touching
failures means two test runs are sharing Mongo; the script's private replica
set should prevent it, but if seen, re-run before investigating the code.

- [ ] **Step 2: Bring up the worktree stack**

```bash
./ops/worktree-up.sh
```

- [ ] **Step 3: Verify by eye at localhost:5273/metrics**

Neither of these has a test that can catch it — the repo has no headless
component testing, by design.

- The splash (`BioFlow` title, blurb, library stats) is **gone** from the
  right side. This is the issue's first complaint.
- The right column shows one table per job type with up to 5 runs each.
- A type with more than 5 runs shows a "See all N →" link; one with fewer
  does not.
- The link lands on `/metrics/:jobType` and pages correctly.
- Short runs show `—` in Peak memory, not `0`.

- [ ] **Step 4: Check a real database, not only the fixtures**

CLAUDE.md's standing warning: these tests feed hand-built objects that already
look the way the code expects. Confirm against real rows.

```bash
docker compose -p biopipe-worktree exec api python -c "
import asyncio
from app.services import timing_service
async def main():
    from app.db.client import connect_to_mongo
    await connect_to_mongo()
    by_type = await timing_service.recent_runs_by_type(limit=5)
    for t, e in by_type.items():
        print(t, 'total=', e['total'], 'shown=', len(e['runs']),
              'outcomes=', {r.outcome for r in e['runs']})
asyncio.run(main())
"
```

Expected: every type caps at 5 shown, totals exceed shown where history is
longer, and any type with real failures shows them in the outcome set.

- [ ] **Step 5: Tear down**

```bash
./ops/worktree-up.sh --down
```

- [ ] **Step 6: Push and open the PR**

```bash
git push -u origin HEAD
```

```bash
gh pr create --base main --fill
```

Label the PR `type:feature` and `area:frontend` — `.github/release.yml`
categorizes by label, and an unlabelled PR lands under "Other changes". The
description must carry `Closes #129`.

---

## Notes for the implementer

**Do not merge the PR.** The end state of this task is an open PR; the user
reviews and merges (CLAUDE.md, "Finish work on a branch").

**`docs/TODO.md` needs no entry here** — this work tracks in issue #129, and
the TODO backlog is a separate list. Do not add or close a TODO entry.

**If a rebuild seems not to take**, check that `--build` was passed before
looking for a cause in the code, and confirm the stack is on this worktree
rather than the main checkout.
