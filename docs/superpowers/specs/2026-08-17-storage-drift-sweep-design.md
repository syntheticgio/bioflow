# Storage drift sweep — design

**Issue:** [#412](https://github.com/syntheticgio/bioflow/issues/412)
**Date:** 2026-08-17
**Status:** design approved, implementation plan pending

## Problem

The database and the filesystem can drift apart. Object records live in Mongo;
blobs and report directories live under `BIOINFO_HOME`. An interruption between
a filesystem operation and its database write — a crash, an OOM-killed worker, a
container stopped mid-ingest — strands data on one side. Today the only way to
learn about drift is a pipeline failing on a file the UI says exists.

This design adds a **read-only** sweep that reports drift as a list the user can
look at deliberately. No deletion, per the scoping decision in #412: a sweep that
deletes is a sweep that can delete the wrong thing due to a bug in the sweep
itself, and the value here is visibility.

## What already exists

The issue was written as though no maintenance machinery existed. It does. Five
scheduled jobs run today (`backend/app/queue/scheduler.py`):

| Job | Interval | Covers |
|---|---|---|
| `verify_files` | 60s | Stats every blob oldest-first; two consecutive misses → `BlobState.MISSING` |
| `gc_blobs` | 600s | Deletes blob records at `ref_count <= 0` past `GC_GRACE` |
| `reap_uploads` | 300s | Staging directories older than 24h |
| `reap_report_dirs` | 3600s | Report directories with no DB record, 1h grace |
| `reap_pipeline_scratch` | 3600s | Pipeline scratch, 6h grace |

Mapped against #412's four requested categories:

1. **Blobs with no record** — `gc_blobs` handles records with no refs, but
   nothing walks `objects/` looking for files with no `Blob` row. **Missing.**
2. **Records with no files** — `verify_files` already detects this, with a
   better algorithm than the issue proposes (two-strike, tolerating transient
   unmounts of external drives). It writes `BlobState.MISSING`. Nothing
   surfaces it. **Detection exists; the read surface is missing.**
3. **Report dirs referenced by facts that no longer exist** —
   `reap_report_dirs` covers the *opposite* direction (a directory with no
   record). The issue's direction is **missing**.
4. **Reclaimable bytes** — depends on (1).

The issue's implementation notes (age threshold, exclude staging, safe under
concurrent jobs) are already-solved problems; `reap_report_dirs` is a working
worked example of the grace-window pattern to copy.

**Decision:** reuse `verify_files` rather than re-deriving category 2
independently. An independent re-derivation doubles the code that can be wrong,
and there is no second opinion to compare against. The counter-argument — that a
bug in `verify_files` is then inherited silently by a report whose stated value
is trust — is accepted as a known limitation and recorded here.

## Requirements

Each requirement is independently checkable. IDs are permanent and not reused.

### Detection

- **DS-1** — The sweep reports each file under `objects_dir` that has no `Blob`
  record, as category `orphaned_file`.
- **DS-2** — The sweep reports each file under `objects_dir` whose `Blob` record
  is in state `PENDING` and whose record is older than `GC_GRACE`, as category
  `stalled_ingest`.
- **DS-3** — The sweep does not report a file whose `Blob` record is `PENDING`
  and younger than `GC_GRACE`.
- **DS-4** — The sweep reports each `Blob` record in state `MISSING`, as
  category `missing_blob`, without performing its own filesystem check.
- **DS-5** — The sweep reports each object that claims a report (per **DS-9**)
  whose corresponding report directory does not exist, as category
  `missing_report_dir`.
- **DS-6** — The sweep reports the total size in bytes of all `orphaned_file`
  and `stalled_ingest` entries, as `reclaimable_bytes`.
- **DS-7** — No category includes a `Blob` whose `storage` is
  `BlobStorage.EXTERNAL`. Those files are registered in place, live outside
  `BIOINFO_HOME`, and are never ours to reclaim — so an external blob whose
  registered path has vanished is not `missing_blob`, and its bytes never count
  toward `reclaimable_bytes`. This constrains **DS-4** (query filtered to
  `MANAGED`) and **DS-6**; **DS-1** and **DS-2** are unaffected, since a file
  found under `objects_dir` is by construction managed storage.
- **DS-8** — The sweep reads only `objects_dir` and the report roots. It never
  walks `staging_dir`, `tmp_dir`, `ncbi_dir`, `lineages_dir`, or
  `agent_sessions_dir`.

### The report-root mapping

- **DS-9** — An object "claims a report" when the fact the UI gates that
  report's tab on is present. The mapping is:

  | Predicate fact | UI gate | Report root |
  |---|---|---|
  | `qc_tool` | `typeof facts.qc_tool === "string"` (`DetailPanel.tsx:619`) | `qc_reports_dir` |
  | `bam_stats_summary` | presence (`BamResults.tsx:98`) | `bam_stats_dir` |
  | `vcf_stats_summary` | presence (`VariantResults.tsx:42`) | `vcf_stats_dir` |
  | `annotation_stats_status` | `=== "ok"` (`AnnotationResults.tsx:40`) | `annotation_stats_dir` |

  The predicates are deliberately **not uniform**, because the UI's gates are
  not uniform. Keying on the UI's predicate rather than the handler's
  `*_status` fact is what makes **DS-5** match the failure the user actually
  experiences: a visible tab that fails when opened. Keying on `*_status`
  would report objects the UI never offers a tab for and miss objects showing
  a broken one.

- **DS-10** — `transcript_qc_status` is a report status fact with no report
  directory; its results live entirely in facts. It belongs to a companion
  frozenset `REPORTS_WITHOUT_DIRS`, not to the mapping.
- **DS-11** — A test asserts
  `set(all_report_status_facts) == set(REPORT_ROOTS) | REPORTS_WITHOUT_DIRS`.
  This is the "genuinely derivable" registry pattern from `CLAUDE.md`: without
  it, a new report type nobody adds to the mapping is silently never checked.
- **DS-12** — A test asserts, per report type, that the UI predicate and the
  handler's `*_status` fact agree on a fixture object where the report
  succeeded. Divergence is a real bug worth surfacing.

### Execution and surface

- **DS-13** — The sweep runs as a scheduled maintenance job that stores its
  result, not as work performed inside an HTTP request. A full walk of a few
  hundred thousand files must not block a request.
- **DS-14** — The settings page displays the stored result of the most recent
  sweep, including the timestamp at which it ran.
- **DS-15** — The sweep calls `ctx.check_cancel()` during its walk, as
  `reap_report_dirs` does, so a long sweep can be cancelled.
- **DS-16** — The sweep performs no deletion, of any kind, in this version.

### Non-functional

- **Performance** — The sweep walks `objects_dir` in a thread
  (`asyncio.to_thread`) so it never blocks the event loop, matching
  `reap_report_dirs`. It is a maintenance-class job (`JobClass.MAINTENANCE`,
  `IoClass.LIGHT`) and is scheduled accordingly.
- **Correctness under concurrency** — Zero false positives while jobs are
  running is the acceptance bar, because a report that cries wolf will be
  ignored. The `GC_GRACE` window (**DS-2**, **DS-3**) and the `PENDING`-state
  check are what deliver it. Blob records are inserted `PENDING` *before* bytes
  are placed (`blob_service.create_blob_record`), so record-before-file is the
  invariant the detector relies on: a file with no record at all is a genuine
  anomaly, never a race.
- **Capacity** — The report stores counts and per-category entry lists. Entry
  lists are capped so a pathological drift state cannot produce an unbounded
  document; the count remains exact above the cap.

## Design

### Module layout

A new `backend/app/services/drift_service.py` holds the detectors and the
aggregation. It is a service, not a handler, so it can be exercised directly in
tests without going through the queue. The handler
(`backend/app/queue/handlers.py`) is a thin wrapper that calls it, matching how
`reap_report_dirs` delegates deletion to `object_service.remove_report_dirs`.

Three detector functions, each independently testable:

- `find_orphaned_files()` → `orphaned_file` + `stalled_ingest` (**DS-1**–**DS-3**)
- `find_missing_blobs()` → `missing_blob`, reading `BlobState.MISSING` (**DS-4**)
- `find_missing_report_dirs()` → `missing_report_dir` (**DS-5**, **DS-9**)

`sweep()` composes them into one stored result.

### Data flow

1. Scheduled tick enqueues `sweep_storage_drift`.
2. Handler calls `drift_service.sweep()`.
3. `find_orphaned_files` walks `objects_dir` two levels deep (the sharding from
   `blob_rel_path`), batching `Blob` lookups by digest rather than one query per
   file.
4. `find_missing_blobs` queries `Blob.state == MISSING`, filtered to
   `BlobStorage.MANAGED` (**DS-7**).
5. `find_missing_report_dirs` queries objects carrying each predicate fact and
   stats the corresponding directory.
6. The composed result is written to a single-document collection (latest sweep
   only — history is not a requirement, and #412 asks for a list to look at, not
   a trend).
7. `GET /api/v1/maintenance/drift` returns the stored document.
8. The settings page renders it with its timestamp.

### Error handling

The sweep is best-effort and never raises into the queue. A directory that
cannot be read logs and is skipped, matching `remove_report_dirs`' philosophy: a
partial report is worth more than no report. `check_home()` is called first and
the sweep returns `{"skipped": True, "reason": ...}` when the storage home is
not mounted — exactly as `reap_report_dirs` does, and for the same reason: every
blob looks missing when the drive is not there.

### Testing

Unit tests per detector, constructing each drift condition deliberately:

- delete a blob file out from under a `PRESENT` record → `missing_blob` after
  `verify_files` marks it
- drop a file into `objects_dir` with no record → `orphaned_file`
- insert a `PENDING` record older than `GC_GRACE` with a file → `stalled_ingest`
- insert a `PENDING` record younger than `GC_GRACE` → not reported (**DS-3**)
- an object with `qc_tool` and no `qc_reports_dir/<id>/` → `missing_report_dir`
- an `EXTERNAL` blob whose registered path is gone → not reported (**DS-7**)

Plus the two registry tests (**DS-11**, **DS-12**).

Per `CLAUDE.md`, unit tests alone are not the bar for a rule like this: the
suggestion-rules precedent is that hand-built fixtures pass while real data
exposes the wrong answer. Verification includes running the sweep against a real
project with active jobs and confirming zero false positives.

## Out of scope

- **Deletion / reclaim.** Deliberately deferred. Once the report has been
  correct on real data for a while, a guarded "reclaim orphaned blobs" action
  with explicit confirmation becomes a reasonable follow-up.
- **Sweep history / trends.** Latest result only.
- **`annotation_stats_dir` omissions in `remove_report_dirs` and
  `copy_report_dirs`.** Found while reading for this design; filed separately as
  [#481](https://github.com/syntheticgio/bioflow/issues/481). The sharing bug
  there does not self-heal and is independent of this work — though it is an
  instance of exactly the drift **DS-5** detects.

## Decisions and their reasoning

- **Reuse `verify_files` (category 2) rather than re-derive.** Less code that
  can be wrong; the two-strike algorithm is better than what #412 proposed.
  Accepted cost: a `verify_files` bug is inherited silently.
- **Split "orphaned file" into two categories.** `orphaned_file` and
  `stalled_ingest` have different causes and different fixes; collapsing them
  makes the report less actionable.
- **Key the report mapping on the UI's predicate, not the handler's
  `*_status`.** Matches the failure the user experiences. See **DS-9**.
- **Scheduled job with a stored result, not on-demand.** #412 asks for
  incremental-friendliness on large trees; an on-demand full walk would block a
  request.
- **Report only.** #412's own scoping call, retained.
