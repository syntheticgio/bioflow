# Metrics page: per-run tables and its own layout

Design for [#129](https://github.com/syntheticgio/bioflow/issues/129).

## The two problems, diagnosed

The issue reports splash-like content on the right of the Metrics page, and
the absence of per-job-type run tables. Both were guesses at a cause; both
turn out to have a single concrete explanation, and they resolve into one
change rather than two unrelated ones.

**The splash is a real component, not CSS bleed.** `/metrics` is not in the
`singleColumn` list in `App.tsx:79-86` — `/help/*` is, but `/metrics` routes
separately — so the shared `DetailPanel` mounts beside it. `DetailPanel` with
no `?sel=` query param (always the case on `/metrics`) falls through to
`EmptyDetail`, whose own docstring calls it "BioFlow's de facto splash
screen": `.splash-title`, `.splash-blurb`, `.splash-stats`. That is the
reported content, verbatim. The issue speculated it "may be shared page
chrome or CSS rather than an actually-embedded component" — it is the
embedded component.

A second, smaller contributor: `Metrics.tsx` uses `.help-page`, which sets
`max-width: 760px` (`styles.css:2653`) — a prose measure holding an
eight-column data table.

**The per-run tables cannot be built from the current API.** `GET /metrics`
returns one *aggregate* row per job type (median/p90 duration, memory, input
size, tool counts) via `timing_service.metrics()`. There are no individual
runs in the response, which is why the "cap at 5 rows with a see more" ask
had nothing to cap — there is exactly one row per type today. Individual runs
exist in the `job_timings` collection but nothing exposes them per job type.

## Layout

Two columns, no selection state:

- **Left** — the existing aggregate by-job-type table, unchanged. It stays
  the cross-type comparison view.
- **Right** — a stack of per-job-type tables, one after another, each showing
  that type's 5 most recent runs and a "see more" link.

The right column is where the splash currently renders. Replacing it is what
resolves the first half of the issue.

Clicking a left-hand row drives nothing. Every job type's recent runs are
already visible on load, so there is no state to select and no empty first-
load state to design around.

"See more" navigates to `/metrics/:jobType`, a page showing all runs for that
one type with paging.

## Backend

One endpoint, serving every job type in a single request:

```
GET /jobs/metrics/runs                      -> 5 recent runs for every type
GET /jobs/metrics/runs?job_type=X&limit=&offset=  -> one type, paged
```

Serving all types at once matters: a page rendering N tables that each fetch
their own rows would fire N requests on load.

Per-run fields: `finished_at`, `outcome`, `duration_ms`, `input_bytes`,
`peak_rss_bytes`, `threads`, `tool`, `tool_version`.

**Failures are included, and this is the design decision to get right.** A
new accessor `runs_for_type()` sits in `timing_service` alongside
`records_for_object()`, explicitly named so that opting out of the outcome
filter is a visible choice rather than an omission — the convention CLAUDE.md
sets under "Querying computation records". It must not route through
`_modelled()`, whose whole job is excluding failures so they cannot bias the
predictive fits.

This creates a deliberate asymmetry on one page, worth stating because it
looks like an inconsistency: the left table's medians describe successful
runs only, while the right tables list runs of every outcome. That is the
same split `metrics()` already makes between its summaries and its outcome
counts, for the same reason — a metrics page that hid failures would be a
status page for a rosier app.

The existing `model_samples` index (`job_type + outcome + finished_at`)
already covers the query; no new index.

## Frontend

1. Add `/metrics` and `/metrics/:jobType` to `singleColumn` in `App.tsx`, so
   the splash `DetailPanel` stops mounting.
2. `Metrics.tsx` becomes the two-column layout above.
3. New route `/metrics/:jobType` for the see-more destination.
4. New `.metrics-page` class replacing `.help-page`, freeing the table from
   the 760px prose measure. Follows the existing `.software-page` precedent
   (`styles.css:2770`) of overriding the measure for non-prose content.

Absent numbers render as an em-dash, never `0`, per the existing convention
in `Metrics.tsx` — a null is the lack of a measurement, not a measurement of
nothing. This will be visible: `peak_rss_bytes` is null for every run under
the 60s sampling floor, so the memory column will be mostly dashes on short
jobs. That is correct, not a rendering bug.

## Testing

Backend, in `backend/tests/`:

- A failed run appears in `runs_for_type()` output. This is the assertion
  that fails if someone later rewires the accessor to `_modelled`, which is
  the realistic regression.
- `limit`/`offset` paging returns the expected window.
- An unknown job type returns an empty list rather than erroring.
- The unfiltered call returns at most 5 runs per type, covering every type.

Frontend has no headless component testing in this repo and none is expected;
verification is manual at localhost:5273 via `./ops/worktree-up.sh`. Two
things to confirm by eye, since neither has a test that can catch it: the
splash is gone from the right column, and a type with more than 5 runs shows
the see-more link.

## Out of scope

Filtering or sorting the per-run tables, charting run history over time, and
extending the Activity view. The see-more page is a plain paged table.
