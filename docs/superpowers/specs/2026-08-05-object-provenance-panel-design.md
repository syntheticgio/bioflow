# Per-object computation provenance

One owner-scoped route and one History tab showing what computation produced a
file and what computations have since consumed it, failures included.

Issue: [#9](https://github.com/syntheticgio/bioflow/issues/9). Backlog source:
`docs/TODO.md`, "No provenance panel for computation records".

## Why this, and what the data actually says

`timing_service.records_for_object()` (`app/services/timing_service.py:197`)
already returns every run that touched an object, failures included, and is
covered by `backend/tests/queue/test_record_outcomes.py`. The issue frames the
remaining work as "add a route and a panel". Against the real `biopipe`
database that framing is wrong in a way that would ship a working, empty
feature.

111 rows in `job_timings` on 2026-08-05:

| field | rows populated |
| --- | --- |
| `object_id` | **1** |
| `job_id` | 1 |
| `machine.cpu_model` | 1 |
| `tool` / `tool_version` | 0 |
| `threads` | 0 |
| `format_kind` / `compression` | 0 |
| `resources.peak_rss_bytes` | 0 |
| `features` | 0 |

Outcomes: 110 succeeded, 1 failed. The single row carrying an `object_id` is
that failure -- a 30 ms `ingest_headers` recorded today.

The reason is not corruption. `executor._record_timing`
(`app/queue/executor.py:301`) is the only caller of `timing_service.record()`,
and it started passing `job_id`, `object_id` and `machine` when the
computation-records design landed on 2026-08-03. Everything older predates
those arguments. But three gaps are live bugs rather than history, and each
blanks a column the issue's acceptance criteria name:

1. **`tool` and `tool_version` are never passed at all.** `record()` accepts
   both; the executor omits both. The tool name is in the payload the executor
   already holds -- `payload["tool"]` for `trim_reads`, `payload["aligner"]`
   for `align_reads` and `build_index` (verified on real job documents).
2. **`threads` reads a key that does not exist.** The executor does
   `job.payload.get("threads")`. Real payloads nest it: a real `align_reads`
   job has `payload["params"]["threads"]`, as does `quantify`. The top-level
   key is never set by any launcher, so this is `None` on every row and always
   has been.
3. **Records are skipped entirely when size is unknown.** `if not size:
   return` drops the row for any job whose payload has no `size` and whose
   object has none either. Defensible for the models -- there is nothing to
   correlate -- but provenance loses a run that genuinely happened, silently.

Fixing 1 and 2 is in scope here, per the decision recorded below. 3 is not.

## The inversion the issue does not mention

A record's `object_id` is `job.object_id` -- the job's **input**. For
`align_reads` that is the R1 FASTQ; the BAM the run produced gets no record,
ever. Same for `quantify` -> counts, `assemble_upload` -> assembly,
`call_variants` -> VCF.

That inverts the feature. The panel on a derived object -- the place a user
most wants to read "what made this, how long did it take, on what machine" --
would be permanently empty, while the record sits on the FASTQ under a heading
that reads like "runs that touched this file".

**This needs no write-path change.** `DataObject.produced_by_job`
(`app/models/object.py:257`) already exists and is set by nearly every applier
in `app/queue/results.py` -- 33 of 49 real objects carry it. The produced-by
record is a lookup by `JobRunTiming.job_id`, not a new field and not a new
registry.

That last point matters given CLAUDE.md's "hand-maintained registries keyed by
an enum" section. The obvious alternative -- have each applier report the
object ids it created so the executor can record against them -- is exactly the
silent-skip shape that cost the STAR `build_index` job its sidecars: twenty
appliers, and one that forgets to report loses provenance with nothing failing.
`produced_by_job` is already written at the site that creates the object, so it
cannot drift from it.

## Scope

In:

- `GET /api/v1/objects/{object_id}/computations`, owner-scoped, returning both
  halves in one response.
- Executor fixes for `tool`, `tool_version` and `threads`.
- A History tab in the object detail panel with explicit loading, empty and
  error states.

Out, deliberately:

- **Backfilling the 110 historical rows.** They have no `object_id` and no
  `job_id`; there is nothing to join them on. They are not recoverable, and a
  heuristic join on `(job_type, finished_at)` would invent provenance, which is
  worse than admitting the record starts on 2026-08-03.
- **Recording jobs with no known input size.** Gap 3 above. Changing that
  changes what the duration model fits against, and that is a separate
  decision; noted as a follow-up.
- **`params` and `features`.** Sanitized params are recorded but not rendered.
  A run's settings deserve a surface, but it is a different one from a
  six-column history table, and `features` is empty on every row today.
- **A project-wide or installation-wide computation view.** The design named
  three read surfaces; this is the per-object one.

## API surface

```
GET /api/v1/objects/{object_id}/computations?limit=100
```

Owner scoping goes through `object_service.object_with_blob(object_id,
owner=owner)`, the same call `get_object` already makes. This is load-bearing:
`JobRunTiming` has **no owner field**, so nothing in `job_timings` can be
owner-filtered directly. The object fetch is what establishes that this profile
may read these records at all, and it must happen before the records query
rather than alongside it -- an unresolvable object raises before any record is
read.

Response:

```jsonc
{
  "produced_by": { /* ComputationRecord or null */ },
  "records": [ /* ComputationRecord, newest finished_at first */ ],
  "has_more": false
}
```

`produced_by` is separate from `records` rather than being the first element of
a merged list. The two answer different questions ("what made this file" versus
"what has been run on it"), the UI labels them differently, and merging them
would need a per-row discriminator anyway. When `produced_by_job` is set but no
timing row carries that `job_id` -- true for every object created before
2026-08-03 -- it is `null`, indistinguishable from an object nobody produced.
Acceptable: the tab's copy covers both cases without claiming either.

`ComputationRecord` is a new response model in `app/api/v1/schemas.py`, built
from `JobRunTiming` with a classmethod constructor like the other `*Out`
models:

| field | source | note |
| --- | --- | --- |
| `job_type` | `job_type` | |
| `outcome` | `outcome` | one of `RunOutcome`'s four values |
| `finished_at` | `finished_at` | nullable; sort key |
| `duration_ms` | `duration_ms` | |
| `queued_ms` | `queued_ms` | |
| `threads` | `threads` | null until the executor fix lands |
| `tool` / `tool_version` | `tool`, `tool_version` | same |
| `peak_rss_bytes` | `resources.peak_rss_bytes` | null under the 60 s floor |
| `peak_cpu_percent` | `resources.peak_cpu_percent` | same |
| `machine` | `machine.cpu_model`, `logical_cores`, `total_ram_bytes`, `platform` | flattened; `machine_id` omitted |
| `job_id` | `job_id` | lets the UI link to the job's log |
| `input_bytes` | `input_bytes` | |

`params`, `features`, `worker_id`, `project_id`, `format_kind` and
`compression` are not exposed. `machine_id` is deliberately dropped: it
identifies the installation and adds nothing a user reads.

### Pagination

`limit: int = Query(100, le=500)`, matching `jobs.py:125`, plus a `has_more`
boolean. No offset parameter and no pager in the UI.

The issue asks for pagination "unless the implementation proves otherwise",
and the data proves otherwise: the busiest object in the real database has one
record, and an object that has been ingested, QC'd, trimmed, aligned, indexed,
quantified and summarized accumulates under a dozen. A `Load more` control
would exist to page a list that fits on screen.

`has_more` is there so that adding `offset` later is additive rather than a
shape change. It is computed by requesting `limit + 1` and truncating, which
also means `records_for_object` grows a `limit` parameter -- its current
unbounded `.to_list()` is fine at today's volume and is the one thing here that
degrades badly if a single object ever accumulates thousands of runs.

## Executor fixes

In `_record_timing` (`app/queue/executor.py:348`), at the `record()` call:

- `threads=job.payload.get("params", {}).get("threads") or
  job.payload.get("threads")`. The nested read first, the top-level as
  fallback, because `assemble_upload` and the download handlers may grow a flat
  one and neither shape is wrong.
- `tool=` from a small resolution that prefers `payload["tool"]`, then
  `payload["aligner"]`, then `payload["assembler"]`. Explicitly *not* a
  per-job-type mapping dict -- that is the silent-skip registry shape again,
  and a job type missing from it would blank the column with nothing failing.
  Reading whichever key is present degrades to `None` for job types that name
  no tool, which is the honest answer for `ingest_headers`.
- `tool_version=` from `app/pipelines/tools.py` for the resolved tool name.
  This must read the cache only, never trigger a probe. `tools.py` keeps
  `_seeded` (`tools.py:112`), populated at startup from Redis precisely because
  probing is expensive -- NanoPlot alone costs 12 s -- but a miss, or a binary
  whose fingerprint no longer matches, falls through to a `subprocess` call.
  `_record_timing` runs in the executor's `finally`, so a probe there delays
  every job's completion by however long the slowest tool takes to answer
  `--version`. A cache miss records `None`; a version that goes missing is
  worth less than a job that hangs on telemetry.

These are one commit, separable from the route and the tab, and testable on
their own.

## UI

A fourth tab in `DetailPanel`'s tab list (`components/DetailPanel.tsx:330`),
labelled **History**, after Metadata and before Actions. Not a section inside
Metadata: the tab bar is where a user looks for "another view of this file",
and burying it under metadata makes provenance something you find by scrolling.

The name is History, not Computations: `components/Computations.tsx` is already
the tool-picker in the Actions tab, and two panels called the same thing --
one listing what you can run and one listing what has run -- is a naming
collision a reader hits before they hit the code.

Hint on the tab, following the existing pattern where there is something true
to say: the record count, or nothing when there are none.

Layout, in `components/ComputationHistory.tsx`:

- **How this file was made** -- the `produced_by` record as a labelled block,
  not a table row. Absent entirely when `produced_by` is null.
- **Runs on this file** -- a table, newest first. Columns: when, job type,
  tool and version, duration, threads, peak RSS, machine, outcome.
- Outcome renders as a badge. A failed run is the point of the panel, so it is
  styled to be found, not to be tolerated -- `failed` and `dead` are visually
  distinct from `succeeded`, and `cancelled` is neutral.
- A null cell renders as an em-dash, never as `0` or a blank. A run under the
  60 s sampling floor genuinely has no RSS measurement, and `0 B` would be a
  measurement.

### The empty state is the default state, and must say why

Every object but one has no records at all. This is not an edge case to be
handled; it is what nearly every user will see on first open, and for months on
older files.

So the empty state must distinguish two things it would otherwise conflate:

- **Nothing has run** -- no records, and the object has no `produced_by_job`.
  "No computations have been recorded for this file."
- **Runs happened before recording started** -- the object has a
  `produced_by_job` (or its facts show QC results) but no timing row exists.
  Copy must not say nothing ran, because something demonstrably did. It says
  computation records began on 2026-08-03 and earlier runs were not recorded.

The route already returns enough to tell these apart: `produced_by` being null
while the object carries a `produced_by_job` is precisely the second case, so
the response carries a `produced_by_job` id alongside `produced_by` for the UI
to branch on.

Loading and error states follow whatever the neighbouring tabs already do
rather than inventing a third convention.

## Testing

Backend, in `backend/tests/api/`:

- The route returns records newest-first, failures included. A `failed` row
  present in the response is the assertion that matters -- it is the whole
  reason `records_for_object` exists.
- Owner scoping: profile B gets a not-found for profile A's object, and gets it
  *before* any record is read. Assert the negative direction; the positive one
  passes whether or not the scoping works.
- `produced_by` resolves from `DataObject.produced_by_job` to the timing row
  with the matching `job_id`, and is null when no such row exists.
- `has_more` is true at `limit + 1` records and false at exactly `limit`.
- Null resource fields survive serialization as null, not as 0.

Executor, in `backend/tests/queue/`:

- A job with `payload["params"]["threads"] = 8` records `threads == 8`. This
  test fails against today's code, which is the point.
- A `trim_reads` payload records `tool == "fastp"`; an `align_reads` payload
  records the aligner; an `ingest_headers` payload records `tool is None`.

Against the real database, per CLAUDE.md -- fixtures here are especially
untrustworthy, since a hand-built `JobRunTiming` looks nothing like the mostly
null rows that actually exist:

```bash
docker compose exec api python -c "..."   # from the main checkout root
```

Check that the route returns something sane for object
`6a6f64490d673b9d20bbeeab` (the one row with an `object_id`, a failure) and an
honest empty for a BAM with a `produced_by_job` predating 2026-08-03.

Manual verification at localhost:5273 via `./ops/worktree-up.sh`: open the
History tab on a file with no records, on the failed-record object, and on a
derived BAM. Then run a fresh QC job and confirm a row appears with a tool
name, a thread count, and a machine -- that run is the first real end-to-end
check that the executor fixes and the read path agree.

## Follow-ups

- **Jobs with no known input size are never recorded** (gap 3). Affects the
  models as well as provenance; needs its own decision.
- **Tool version probing.** If `tools.py` cannot supply a cached version, the
  column stays null until something can, without a subprocess in the executor's
  `finally`.
- **Params surface.** Sanitized run settings are recorded and rendered nowhere.
- **Project-wide computation view**, the second of the design's three read
  surfaces.
