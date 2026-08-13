# Automatic annotation analysis at ingest

Design for [#298](https://github.com/syntheticgio/bioflow/issues/298).

[#257](https://github.com/syntheticgio/bioflow/issues/257) computes annotation
results on demand: opening a GFF3's Results tab shows a "Compute results"
button, and the numbers appear after a queued full-file pass. That kept
ingestion bounded, which was the right call for #257 and is explicitly listed
there as the reason ingest-time parsing was rejected.

The cost is that every newly ingested annotation opens to a button. This
design makes the analysis run automatically at ingest, so the first Results
visit shows results.

## What exists today

`run_annotation_stats` (`backend/app/queue/annotation_handlers.py`) reads the
file once, building both the aggregate facts and the SQLite feature table.
`launch_annotation_stats` (`backend/app/services/pipeline_service.py:2488`)
resolves the file path and the reference contig lengths, then enqueues with
`dedup_key=f"annotation_stats:{ann.id}"`.

The only caller is `POST /pipelines/annotation-stats`
(`backend/app/api/v1/pipelines.py:932`), reached from the "Compute results"
button in `AnnotationResults.tsx`.

`_apply_ingest_headers` (`backend/app/queue/results.py:99`) is where an
uploaded file becomes `READY`. It already ends with conditional follow-up
work — the reference-role assignment and `_link_mate`.

## Measurements

Taken against this machine's real database on 2026-08-13, before any of this
shipped.

**What is actually in there — 13 objects across the four annotation formats:**

| Format | Count | Size range | Real annotations? |
|---|---|---|---|
| GFF | 5 | 1.8–12.5 MB | Yes |
| BED | 8 | 407 B – 2.2 KB | **No — all sidecars** |
| GTF | 0 | — | — |
| GenBank | 0 | — | — |

All 8 "BED" objects are `.fai` indexes and STAR `.ann` index files
misdetected as BED by the format detector. Every one carries
`sidecar_of != None`. This measurement is the reason for the sidecar guard
in R3 below; it was not anticipated before looking.

All 13 have `annotation_stats_status = None` — #257 shipped the day before
and nobody has pressed the button.

**Cost of one full run**, on the largest real GFF (12.5 MB, 51,793 features):

| Phase | Time |
|---|---|
| Parse | 0.25 s |
| SQLite build | 0.40 s |
| Hierarchy resolve + gene table | 0.43 s |
| **Total** | **0.83 s** |

The resulting `features.db` is 26.1 MB — roughly 2× the source file.

## Decisions

### Universal, not threshold-based or user-configurable

Every non-sidecar annotation is analyzed at ingest. No size threshold, no
setting.

The issue asks whether this should be threshold-based. The measurements say
the threshold would be tuned against the wrong cost. 0.83 s of background
CPU on a 12.5 MB annotation is not a latency anyone experiences — the job is
queued, and the object is `READY` before it starts. The real cost is the
26 MB database, and a size threshold responds to that badly: it would refuse
to compute exactly the large-file results a user is most likely to wait for,
while saving disk on files that are cheap either way.

Rejected alternatives:

- **Threshold on source size, auto below / manual above.** Backwards. The
  large files are where waiting hurts most, and 12.5 MB still finished in
  under a second.
- **A user-facing setting.** A knob whose correct value is "on" for every
  measured case, and which every user would have to learn about to benefit
  from the feature.

If disk growth becomes a real problem, the fix is eviction of stale
annotation databases (see [Out of scope](#out-of-scope)), not a policy that
declines to compute results.

### Two trigger sites, because one loses a race

The obvious design — fire when the annotation becomes `READY` — is correct
only when the annotation's reference is already `READY` too.

`resolve_annotation_reference`
(`backend/app/services/pipeline_service.py:2587`) tier 2 matches on
`ncbi_assembly_accession` among candidates filtered to
`status=ObjectStatus.READY` and `role=ObjectRole.REFERENCE`. On the most
common path that creates an annotation — an NCBI assembly download staging
`genomic.fna` and `genomic.gff` together — both files ingest concurrently,
and the FASTA's `REFERENCE` role is assigned at the *end* of its own
`_apply_ingest_headers`, after a network enrichment lookup that takes
seconds.

So an annotation that wins that race resolves no reference and is analyzed
with no contig lengths. Nothing fails. `covered_fraction` and `per_mb` come
back `null` for every contig
(`backend/app/pipelines/annotation_stats.py:182`), the coverage chart draws
blank bars, and the track viewer refuses to draw an axis. The user sees an
authoritative-looking result that is missing half its content, with nothing
saying why.

Manual launch never hit this: a human presses the button minutes later, when
everything is `READY`.

The fix is a second trigger. When a FASTA becomes `READY` with the
`REFERENCE` role, the annotations in its project that wanted a reference are
launched then.

Rejected alternative: **trigger once after the NCBI staging batch settles.**
More precise for the download path, but it couples the trigger to that
handler's internals and still leaves the standalone case wrong — a user who
uploads a GFF on Monday and its genome on Tuesday gets a referenceless
analysis with no repair. The two-trigger rule handles both orderings with
one idea.

### An explicit fact, not an inferred one

Trigger 2 needs to find annotations that were analyzed without a reference.

`annotation_contig_lengths_known: bool` is recorded in facts, decided by the
launcher (which already computes `lengths` at
`pipeline_service.py:2519`), passed on the payload, and returned by the
handler.

Rejected alternative: **infer it from `annotation_per_contig[*].length ==
null`.** No new fact, but the query becomes an `$elemMatch` over an array
that holds one entry per contig — thousands on a scaffolded assembly — and
it cannot distinguish "no reference at all" from "reference resolved but
missing this contig", which are different situations that should not be
repaired the same way.

The explicit fact also fixes a live gap: `AnnotationResults.tsx` currently
renders a referenceless analysis as blank coverage bars with no explanation.

### Backfill treats never-analyzed and referenceless alike

Trigger 2 launches for annotations that either have no
`annotation_stats_status` or have `annotation_contig_lengths_known == false`.

Restricting it to the referenceless ones would assume trigger 1 always fired,
which is false for the 5 GFFs already in this machine's database from before
this feature exists. A reference landing in a project is a natural moment to
backfill, and the existing dedup key plus 0.83 s of compute makes the
redundancy free.

## Requirements

Identifiers are permanent and not reused.

### Triggering

**R1.** When `_apply_ingest_headers` sets an object to `READY` and that
object's `format.kind` is one of `gff`, `gtf`, `bed`, or `genbank`, the
system enqueues an annotation-stats computation for it.

**R2.** The computation in R1 is enqueued only after the object's status,
format, and facts have been written and its role assignment has run.

**R3.** The system does not enqueue an annotation-stats computation for an
object whose `sidecar_of` is set.

**R4.** When `_apply_ingest_headers` sets a FASTA object to `READY` and that
object's role is `REFERENCE`, the system enqueues an annotation-stats
computation for every object in the same project that satisfies R5.

**R5.** An object qualifies for R4's backfill when its `format.kind` is one
of `gff`, `gtf`, `bed`, or `genbank`, its `sidecar_of` is unset, and either
its facts contain no `annotation_stats_status` or its facts contain
`annotation_contig_lengths_known` equal to false.

**R6.** When a user presses "Compute results" for an object that already has
an automatic computation queued or running, no second job is created.

### The reference-known fact

**R7.** `launch_annotation_stats` records on the job payload whether it
resolved contig lengths for the annotation.

**R8.** `run_annotation_stats` returns `annotation_contig_lengths_known` in
its facts, equal to the value R7 recorded.

**R9.** A user viewing an annotation whose
`annotation_contig_lengths_known` is false can tell from the Results view
that coverage was not computed because no reference was resolved.

### Failure isolation

**R10.** An object that reaches `READY` remains `READY` when the
annotation-stats computation enqueued for it cannot be enqueued.

**R11.** An object that reaches `READY` remains `READY` when the
annotation-stats computation enqueued for it fails.

**R12.** A failure to enqueue an automatic annotation-stats computation is
recorded in the application log with the object's identifier.

### Non-functional

**R13.** An object reaches `READY` without waiting for its annotation-stats
computation to run.

**R14.** The backfill in R4 issues one project-scoped object query
regardless of how many annotations the project holds.

**R15.** A project holding annotations that all qualify under R5 completes a
reference ingest without the applier's duration growing in proportion to the
number of annotations analyzed — the analyses run as queued jobs, not inline.

## Architecture

No new handler, no new parser, no new job type, no new endpoint. #257's
`run_annotation_stats` and `launch_annotation_stats` do the work unchanged.

```
ingest_headers completes
  └─ _apply_ingest_headers
       ├─ writes status/format/facts, assigns role   (unchanged)
       ├─ [trigger 1] annotation, not a sidecar
       │                → launch_annotation_stats(obj)
       └─ [trigger 2] FASTA with role REFERENCE
                      → for each qualifying annotation in project:
                          launch_annotation_stats(ann)
```

Both triggers call the same launcher the button calls. Queue priority,
deduplication, cancellation, retry, and visibility in the Computations panel
are therefore identical to the manual path and require no new code:
`job_class=JobClass.COMPUTE`, `max_attempts=2`,
`dedup_key=f"annotation_stats:{ann.id}"`.

### Where the code goes

- `backend/app/queue/results.py` — both triggers, as a helper called from
  the end of `_apply_ingest_headers`. The existing `then_bam_stats` chain at
  `results.py:1518` is the shape to follow, including its
  `try/except AppError` and warning log.
- `backend/app/services/pipeline_service.py` — `launch_annotation_stats`
  gains the payload flag (R7); a new predicate answers R5 so the rule lives
  in one place and is testable without the queue.
- `backend/app/queue/annotation_handlers.py` — returns the fact (R8).
- `frontend/src/components/AnnotationResults.tsx` — the refusal note (R9).

### Error handling

Both triggers are wrapped in `try/except AppError` with a warning log, and
both run after `obj.set(update)`. The object is already `READY` before
either trigger executes, so nothing a trigger does can unmake that — which
is the issue's stated design constraint: a malformed annotation must not
turn a successfully stored source file into an ingest failure.

The analysis itself remains a separate recoverable computation. A failed job
leaves the object `READY` with no `annotation_stats_status`, the Results tab
falls back to its existing "Compute results" button, and the failure is
visible in the Computations panel like any other job.

## Testing

Backend, in `backend/tests/`:

- Trigger 1 fires for each of `gff`, `gtf`, `bed`, `genbank`.
- **Trigger 1 does not fire for a BED whose `sidecar_of` is set.** This is
  the direction that fails when the guard breaks; a test asserting only that
  real annotations *do* trigger passes whether or not the guard works.
- Trigger 2 fires for a never-analyzed annotation and for one with
  `annotation_contig_lengths_known == false`; does not fire for one with it
  true.
- Trigger 2 does not fire for a FASTA without the `REFERENCE` role.
- A `launch_annotation_stats` that raises `AppError` leaves the object
  `READY` — asserted on the object's status, not on the log.
- `annotation_contig_lengths_known` round-trips false when no reference
  resolves and true when one does.

Against the real database, per CLAUDE.md's "check a rule against the real
database" note — the failure it describes is exactly this shape, rules that
pass green against hand-built fixtures that already look the way the rules
expect:

```bash
docker compose exec api python -c "..."
```

Confirm the R5 predicate selects the 5 real GFFs and none of the 8
sidecars.

Frontend: manual check at localhost:5273 via `./ops/worktree-up.sh`, per
CLAUDE.md — ingest an annotation and confirm the Results tab opens to
results rather than a button, and that a referenceless annotation shows the
R9 note.

## Out of scope

**Eviction of annotation databases.** Universal analysis makes disk growth
real — 2× the source file per annotation, and this machine already holds
three copies of the same 12.5 MB GFF. Nothing cleans up
`annotation_stats_dir` today. That is a genuine follow-up and wants its own
issue; it is not a reason to gate the analysis.

**The BED misdetection.** Eight sidecar files classified as BED is a format
detector bug. This design routes around it with the R3 sidecar guard, which
is the correct guard regardless of what the detector reports — a `.fai` is
not an annotation even if it parses as one. Fixing the detector is separate.

**Re-analysis when an annotation's file changes.** Out of scope here for the
same reason it is out of scope for #257: nothing in this repo re-ingests a
blob in place.
