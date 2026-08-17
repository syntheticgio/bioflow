# Per-project export archive for sharing an analysis

**Issue:** [#476](https://github.com/syntheticgio/bioflow/issues/476)
**Date:** 2026-08-17
**Status:** design approved, ready for implementation plan

## Problem

There is no way to send a collaborator an analysis. A project's value --
what was run, on what, with which versions and parameters, producing which
results -- lives entirely inside one machine's Mongo and `/data`, readable
only through the running app.

This is Tier 2 of [#411](https://github.com/syntheticgio/bioflow/issues/411),
split off because it shares no implementation with Tier 1 and serves a
different goal. #411 protects the research record against loss; this is a
collaboration feature. Scoping them together would have produced one plan
with two disjoint halves.

Two things already in the repo shape this more than anything new:

- **`services/provenance_report.py` already renders a citable methods
  report** from a `ProvenanceChain`. It works with no AI provider configured
  and holds the governing rule: *never omit a step known to have run, and
  never assert a fact that is not there*. The human-readable half of this
  archive is that renderer applied per object, not a new one.
- **`Share` / `share_service.py` is not reusable and must not be extended.**
  It is profile-to-profile on one machine and deliberately moves no bytes --
  both sides point at the same refcounted blob (see
  `docs/superpowers/specs/2026-08-05-profile-sharing-design.md`). Crossing
  machines is precisely the thing it cannot do. It is named here so that a
  later reader does not mistake it for a starting point.

## Scope

One project and its descendants, exported to a single `.tar.gz` archive
produced by a queue job and downloaded through the API.

The archive is **read-only documentation of an analysis**. There is no
import path in this work. The format is designed so an importer remains
possible later; building one is not in scope.

## Decisions

Each of the issue's four open questions, resolved:

| # | Question | Decision |
|---|---|---|
| 1 | Scope; sidecars and report directories? | **One project plus its descendants.** |
| 2 | What does the recipient do with it? | **Read-only now, import later.** Versioned envelope, ObjectIds preserved; no importer built. |
| 3 | Blobs or manifest? | **Selective.** Manifest always lists every blob; bytes included below a size threshold, user-overridable. |
| 4 | Redaction | **Widened.** Secrets, filesystem paths, and machine identity all excluded. |

### 1. One project plus its descendants

Projects nest through `parent_id` and `path` (`app/models/project.py`).
Exporting a parent without its children yields an archive missing most of
the analysis, with nothing in it saying so -- the reader cannot distinguish
"this project had no alignments" from "the alignments were in a sub-project
that was not included."

Multi-project selection and arbitrary object selection were both rejected as
scope that does not serve the feature's story. "Send a collaborator this
analysis" is one project.

**Sidecars and report directories follow their object.** They are part of
the analysis a recipient reads -- a BAM index or an annotation stats
directory is what makes the object usable -- so they are in scope, subject
to the same size threshold as any other bytes (§3) and listed in
`data-manifest.tsv` whether or not their bytes are included. They are not a
separate opt-in: an object included without its sidecars is a trap the
recipient discovers later.

**Known gap: report directories are not yet packed.** Sidecars (`DataObject`s
with `sidecar_of` set, e.g. a BAM index) are covered -- `collect()` picks
them up incidentally because they are ordinary objects. But
`qc_reports_dir`, `bam_stats_dir`, `vcf_stats_dir`, and `annotation_stats_dir`
(`config.py`) are not blobs at all; they live outside `objects/`, keyed by
object id, and the implementation does not currently walk them, add their
bytes to the archive, or add rows for them to `data-manifest.tsv`. This was
flagged in the 2026-08-17 final whole-branch review (I4) as an omission the
spec's own text rules out being silent about -- the README and `report.md`
now say plainly that these directories are not included, and
[#536](https://github.com/syntheticgio/bioflow/issues/536) tracks packing
them properly (mapping each directory to manifest rows, applying the size
threshold, and matching entries to objects is real scope of its own). This
is a deliberate, recorded deferral, not an oversight.

### 2. Read-only now, import later

The recipient reads, checks, or cites the analysis. They may not be running
BioFlow at all -- which is why `report.md` and `data-manifest.tsv` are
plain files readable without it.

Two properties make a future importer possible without building one now:

- **A versioned envelope.** `manifest.json` carries
  `bioflow_export_version`, so a later importer can refuse or adapt.
- **Original ObjectIds retained** in the serialized documents, giving an
  importer stable identity to remap against.

Both cost essentially nothing today -- the documents are being serialized
regardless -- and identity is the one property that is genuinely hard to
retrofit. Reconstructing it later by heuristic is the outcome this avoids.

A full round-trip import contract (conflict rules, blob reconciliation,
version-mismatch behaviour) was rejected as significant spec work for code
nobody runs yet.

### 3. Blobs: selective, with a size-threshold default

The manifest always lists **every** blob in scope, including those whose
bytes are not in the archive, flagged as excluded. This distinction is the
manifest's whole value: the recipient can tell "not sent" from "does not
exist," and knows exactly what to ask for.

Bytes are included below a size threshold by default, overridable by the
user before the job starts. A collaborator usually wants the small derived
results -- VCF, GFF, reports -- not hundreds of gigabytes of FASTQ.

Manifest-only (#411's choice) was rejected here: the issue itself notes a
collaborator archive with no data may be useless in a way a backup manifest
is not. All-blobs-always was rejected as an archive that can take hours or
fail outright.

### 4. Redaction, in three tiers

This archive is *meant* to leave the machine, so #411's rule is restated and
widened.

**Secrets -- never present.** No API-key ciphertext, no Fernet key, nothing
from `.biopipe/`. This is the issue's own Verification bar.

**Filesystem paths -- stripped or relativized.** `external_path`,
`BIOINFO_HOME`, anything under a user home directory. These leak a username
and directory layout, and mean nothing on the recipient's machine.
`rel_path` survives: it is relative by construction and the manifest needs
it.

**Machine and node identity -- dropped.** `JobRunTiming.machine`
(`RunMachine`, including `machine_id`), node records, SSH targets, machine
profiles. Timing *durations* stay -- "this alignment took 40 minutes" is
part of the analysis record; "it ran on `gio-workstation.local`" is not.

#### Exclusion by construction, inverting #411

The exporter names the collections it serializes rather than dumping
everything and subtracting.

This is deliberately the **opposite** of #411, whose `mongodump` takes every
collection with no allowlist. The two are right for opposite reasons, and
the inversion should not be read as an inconsistency:

- For **backup**, the failure mode of a missed collection is silent,
  permanent data loss. Including by default fails safe.
- For **export**, the failure mode is a collection added later that holds
  something sensitive and silently leaves the machine. Excluding by default
  fails safe.

**Backup fails safe by including; export fails safe by excluding.**

#### Redaction is enforced by test, not by care

The test suite greps the entire produced archive for a known Fernet key, a
plaintext API key, and the fixture's absolute paths. This is the same
assertion shape #411 uses and exists for the same reason: it is what keeps
the rule true after someone edits the exporter a year from now.

#### Reporting what was redacted

`manifest.json` records which redaction profile ran, and the job's
completion summary states plainly what was stripped -- for example, "12
absolute paths relativized, machine identity removed from 47 timing
records."

A pre-write preview UI was considered and rejected as more surface than the
guarantee needs. What the user needs is to be able to check what left the
machine; an after-the-fact statement plus the grep test provides that
without a second UI.

## The archive

A single `.tar.gz`:

| Path | Contents |
|---|---|
| `manifest.json` | Envelope: `bioflow_export_version`, app `VERSION`, git SHA, UTC timestamp, project tree, per-collection document counts, blob count and total size, redaction profile applied |
| `metadata/*.json` | Serialized documents -- projects, objects, runs, jobs, timings -- with original ObjectIds retained |
| `data-manifest.tsv` | One row per blob: `blob_id`, `size`, `content_sha256`, `state`, and whether the bytes are present in this archive |
| `report.md` | The human-readable analysis: project description, per-object provenance via `provenance_report.py`, run history |
| `blobs/` | Bytes for included blobs, named by `blob_id`. Absent when nothing met the threshold |
| `README.md` | What this archive is, what it is not, and that it cannot currently be imported |

`data-manifest.tsv` is deliberately the same shape as #411's and readable
with `cut` and `grep` on a machine with no Mongo, no Docker, and no BioFlow.
The recipient is the one person guaranteed not to have the app.

`report.md` uses `provenance_report.py` rather than a second renderer, which
means its gap markers (`**version not recorded**`) carry into the archive.
A reader scanning for "which aligner version" sees the question asked and
unanswered, rather than seeing nothing and assuming it did not matter.

## Mechanics

### The handler

A new `@handler` in the queue registry, `HandlerMode.THREAD` -- not
`SUBPROCESS`. `SUBPROCESS` is for handlers that spawn an external process
and are killed by process group; this handler packs the tarball in-process
with Python's own `tarfile`/`gzip` and spawns nothing, so there is no
subprocess to be killed. It is still I/O-heavy, which is what keeps it off
the event loop -- `THREAD` runs it in the worker-pool thread instead. The
job carries `project_id`, the blob-inclusion threshold, and the destination
path.

### The node-type registry: `EXCLUDED_LAUNCHES`

`app/pipelines/node_types.py` asserts that every `launch_*` is either a
`NodeTypeSpec` or explicitly excluded. Export belongs in
`EXCLUDED_LAUNCHES`: it is a project-level operation producing an archive on
disk, not an object a downstream node could consume -- the same class as
`launch_summary` and `launch_gc_tracks`. The exclusion carries a comment
saying so, matching the surrounding entries.

**Run the whole `TestExhaustiveness` class, not just the one test.** #355
added a `NodeTypeSpec` and an exclusion in two independent commits; both
landed, satisfying the test its issue named while silently failing
`test_no_launcher_is_both_used_and_excluded` in the same class. It stayed
red until someone ran the whole file (fixed in #366).

### Where the archive lands

Under `$BIOINFO_HOME`, in a dedicated exports directory, served back through
a download endpoint. Not `./backups/` -- different lifecycle, different
audience.

Retention is the user's job. This matches #411's reasoning: automatic
pruning is a feature whose bugs delete things.

### API and UI

- `POST` to create the export job.
- `GET` to list exports and download an archive.
- A project-level UI action opening a dialog that shows the blob-inclusion
  threshold and the projected archive size **before** enqueueing.
- Progress appears in run history like any other job, so a large export
  survives a browser refresh.

## Testing

Backend tests via `pytest`. From a worktree this is
`./backend/run-worktree-tests.sh tests/ -q`, never
`docker compose exec api` -- the latter silently tests main's code.

The load-bearing tests:

- **Redaction greps.** The produced archive contains no Fernet key, no
  plaintext API key, and none of the fixture's absolute paths. This is the
  assertion that outlives the people who wrote it.
- **Descendants are included.** A project with a nested sub-project exports
  the sub-project's objects and runs.
- **Excluded blobs are listed as excluded**, not absent, in
  `data-manifest.tsv`.
- **`TestExhaustiveness` in full**, per the #355 trap above.
- **Envelope round-trip.** `bioflow_export_version` present and ObjectIds
  preserved in `metadata/*.json`.

### One manual pass

Before closing the issue: export a real project and read `report.md`. A
fixture cannot catch a provenance shape only real data has -- the same
discipline behind CLAUDE.md's note that a rule should be checked against the
real database, not only its unit tests.

## Files

| File | Change |
|---|---|
| `backend/app/services/export_service.py` | New. Collect, redact, render, pack. |
| `backend/app/queue/pipeline_handlers.py` | New `@handler` for the export job. |
| `backend/app/pipelines/node_types.py` | Add the launcher to `EXCLUDED_LAUNCHES` with a comment. |
| `backend/app/api/v1/` | New endpoints: create, list, download. |
| `frontend/src/` | Project-level export action and dialog. |
| `backend/tests/services/test_export_service.py` | New. The assertions above. |

## Risk

The implementation risk is concentrated in redaction. Packing an archive is
straightforward; guaranteeing that nothing sensitive is in it is the part
that is easy to believe and hard to know. Write the grep assertions before
the exporter is complete, so the guarantee is checked from the first commit
rather than added after the code looks finished.

The second risk is the registry partition -- see `EXCLUDED_LAUNCHES` above.
It is a known trap with a known cost, and running the full
`TestExhaustiveness` class is the whole mitigation.
