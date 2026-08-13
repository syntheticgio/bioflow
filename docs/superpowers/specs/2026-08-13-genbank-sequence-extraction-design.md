# GenBank sequence extraction

Design for [#348](https://github.com/syntheticgio/bioflow/issues/348),
a follow-up to [#294](https://github.com/syntheticgio/bioflow/issues/294)
where this work was explicitly deferred.

#294 added GenBank annotation support and deliberately skipped the `ORIGIN`
sequence block. A GenBank file's nucleotides are therefore unusable today for
anything beyond the annotation itself: the parser records that sequence is
present, but nothing lets a user act on it. This design extracts that sequence
into a first-class FASTA reference.

## What exists today

Verified against `main` at `cd9ade0d`:

- `pipelines/genbank_reader.py` streams records for the feature-table pass.
  Its `ORIGIN` branch (lines 111-116) sets `record.has_sequence = True` and
  discards the line. The module docstring states the guarantee this exists to
  provide: a record's sequence is "stepped over line by line and never
  accumulated", so a `.gbff` whose bulk is sequence costs no more memory than
  one whose bulk is features.
- `queue/annotation_handlers.py:147` records `genbank_has_sequence` as a fact
  on the annotation object.
- **Nothing reads that fact.** It is absent from `frontend/src/api/types.ts`
  (only `genbank_record_count` is present) and appears nowhere in
  `frontend/src`. It is written by the backend and consumed by no one.
- There is no extraction handler, no launcher, and no derived-FASTA path
  anywhere in the tree.

The nearest sibling is `export_annotation_subset`: an on-demand,
user-triggered job that produces a derived object, with
`_apply_export_annotation_subset` (`queue/results.py:1733-1785`) as its
applier. This design follows that shape closely, and each place it departs is
called out below.

## Decisions

### D-1: A first-class derived object, not a sidecar

The extracted FASTA is a `DataObject` with `role=ObjectRole.REFERENCE` and
`derived_from=[genbank.id]`. It is **not** a `SidecarRole`.

The issue's scope — "so the assembly becomes usable as an alignment reference
(or any other FASTA-consuming pipeline)" — requires the object to be
selectable in a reference picker. A sidecar is scaffolding that the explorer
filters out by design; nobody browses to one. Role is what makes an object
selectable, so `role=REFERENCE` *is* the "wire it into the results machinery"
step the issue asks for. No further registration is required.

A consequence worth recording: because this is not a sidecar, `_SIDECAR_ROLES`
is never touched. That registry is the one CLAUDE.md flags as having cost a
silent failure (a `build_index` job reporting success while storing none of
its files), because a member with no entry is skipped rather than raised. This
design puts that failure mode out of scope by construction rather than by
care.

### D-2: A separate module, not a flag on the reader

Extraction lives in a new `pipelines/genbank_sequence.py` with its own
streaming pass. `genbank_reader.py` is not modified to optionally accumulate
sequence.

The reader's memory guarantee is load-bearing for `build_annotation_db` and
documented as such. A flag that makes it sometimes accumulate would weaken a
property three callers depend on, in order to serve one caller that wants the
inverse shape (skip the feature block, stream `ORIGIN`). Two passes with
opposite guarantees are cheaper to reason about than one pass with a mode.

### D-3: Contig naming is shared code, not parallel code

Both modules name a contig `VERSION` → `ACCESSION` → `LOCUS`. That logic is
lifted out of `genbank_reader.flush()` into a helper both call.

This is not tidiness. The reader's own docstring (lines 63-66) explains that
the versioned accession is what NCBI's paired FASTA uses in its deflines, and
that a GenBank and its sibling FASTA *must* agree on contig names because
contig lengths arrive from a reference's facts and are matched by name. Two
hand-maintained copies of a three-tier fallback is exactly the shape that
drifts, and the drift would be silent — lengths simply stop matching.

### D-4: Extraction is idempotent, guarded by a query

Re-extracting from the same GenBank does not produce a second object. Before
queueing, the launcher looks for a `REFERENCE` object whose `derived_from`
contains the source id; if one exists, it is returned rather than queueing a
new job.

Unlike `export_annotation_subset` — where each export carries a different
user-chosen filter and is legitimately a distinct file — extraction has no
parameters. The same GenBank always yields byte-identical FASTA, so a second
run would produce an indistinguishable duplicate of a potentially
multi-hundred-megabyte reference, and both would appear in every reference
picker.

Replacing the object in place was rejected: it would silently mutate a file
that downstream alignments may already reference by id, breaking the very
provenance the issue asks to preserve.

**The guard reads the world, not a stored flag.** There is no
"already extracted" boolean to go stale. Two edge cases fall out of existing
behavior rather than needing new logic:

- Deletion in this codebase is hard, not soft — `delete_object`
  (`services/object_service.py:794`) detaches the blob and the document is
  gone. If the user deletes the extracted FASTA, the guard's query stops
  finding it and the action returns. Re-extraction is a recovery path, not a
  duplicate.
- The query keys on `derived_from` and role, never on name, so renaming the
  extracted reference does not fool it. A renamed reference is still the
  extracted sequence.

### D-5: Deleting the GenBank does not delete the FASTA

This requires no code. `delete_object`'s contract (lines 803-806) already
states that derived files deliberately do not cascade, precisely so that a
derived artifact outlives its source. An alignment built against the
extracted reference therefore survives deleting the source annotation.

## Requirements

Identifiers are permanent and are not reused. Prefix `GS`.

### Extraction

- **GS-1** — Given a GenBank file containing an `ORIGIN` block,
  `extract_genbank_sequence` writes a FASTA file containing that sequence.
- **GS-2** — A multi-record `.gbff` produces one FASTA record per GenBank
  record, in source order.
- **GS-3** — Each FASTA record's defline is `>{accession}`, where accession is
  the record's `VERSION`, falling back to `ACCESSION`, then the `LOCUS` name,
  then `unknown`.
- **GS-4** — For any given GenBank record, the accession `genbank_sequence`
  emits and the accession `genbank_reader` emits are identical.
- **GS-5** — Sequence is emitted with `ORIGIN`'s base counters and intra-line
  spaces removed, wrapped at 60 columns.
- **GS-6** — The handler reads gzipped input transparently, sniffed by magic
  bytes rather than file extension (matching `genbank_reader._open_text`).
- **GS-7** — Peak memory during extraction does not scale with the size of the
  input file's `ORIGIN` blocks.
- **GS-8** — A GenBank file with no `ORIGIN` sequence fails with
  `PermanentError`. The handler determines this by reading the file, not by
  trusting the `genbank_has_sequence` fact, which may predate an edit to the
  source.
- **GS-9** — The output is written under `_prepare_workdir(ctx,
  "genbank_sequence")`, so ingestion is an atomic rename rather than a copy.

### The derived object

- **GS-10** — On success, the FASTA is ingested as a `DataObject` with
  `role=ObjectRole.REFERENCE`.
- **GS-11** — That object's `derived_from` contains the source GenBank's id.
- **GS-12** — That object's `produced_by_job` is the extraction job's id.
- **GS-13** — That object inherits the source GenBank's `metadata` (it
  describes the same biology) and its `owner` and `project_id`.
- **GS-14** — The extraction job's run records the new object via
  `run_service.record_outputs`.
- **GS-15** — The extracted object is offered by any pipeline that accepts a
  FASTA reference, with no per-pipeline change required.
- **GS-16** — The extracted object carries no `SidecarRole`.

### The guard

- **GS-17** — A launch request for a GenBank that already has an extracted
  reference does not queue a second job.
- **GS-18** — In that case the launcher's response identifies the existing
  derived reference.
- **GS-19** — After the extracted reference is deleted, a launch request for
  the same GenBank queues a job.
- **GS-20** — Renaming the extracted reference does not cause a subsequent
  launch request to queue a second job.

### Surface

- **GS-21** — The action appears in the annotation Results tab
  (`AnnotationFeatureTable.tsx`), for GenBank objects only.
- **GS-22** — When `genbank_has_sequence` is false, no extraction action is
  offered.
- **GS-23** — When `genbank_has_sequence` is true and no derived reference
  exists, the tab offers an "Extract sequence" control.
- **GS-24** — When a derived reference exists, the tab shows a link to it in
  place of the control.
- **GS-25** — The control's state and the launcher's guard are computed from
  the same query, so they cannot disagree.

### Classification

- **GS-26** — `launch_extract_genbank_sequence` is classified in
  `node_types.py` as an excluded launch, alongside
  `launch_annotation_export`.

## Architecture

### `pipelines/genbank_sequence.py` (new)

One public function, streaming, mirroring `genbank_reader.iter_records`'
structure with inverted priorities: the feature block is skipped, the `ORIGIN`
block is transformed and written to an output handle as it is read. Never
holds a full record's sequence in memory (GS-7).

Depends on: the shared accession helper (D-3), `gzip`, `pathlib`. Knows
nothing about jobs, objects, or the database.

### `queue/annotation_handlers.py` — `extract_genbank_sequence` (new handler)

`HandlerMode.THREAD` and `JobClass.COMPUTE`, matching both existing handlers
in the module — the work is file I/O in this process with no binary to spawn
or kill by process group. `IoClass.HEAVY`. Memory allocation is modest and
independent of input size, per GS-7.

Returns `{"object_id", "output": {"tmp_path", "name"}}`, the shape
`_apply_export_annotation_subset` already consumes.

### `queue/results.py` — `_apply_extract_genbank_sequence` (new applier)

Modeled on `_apply_export_annotation_subset` (lines 1733-1785). The single
material difference is `role=ObjectRole.REFERENCE` rather than
`ObjectRole.ANNOTATION`. Registered in the applier map alongside
`export_annotation_subset`.

Name: `{genbank_stem}.fna`, with collisions resolved by `ingest_local_file`'s
existing handling.

### `services/pipeline_service.py` — `launch_extract_genbank_sequence` (new)

Runs the D-4 guard, then queues. Endpoint `POST /pipelines/genbanksequence`
in `api/v1/pipelines.py`, following `launch_annotation_stats`'s shape
(`pipelines.py:915-922`). A companion read endpoint returns the existing
derived reference or null, serving GS-25.

### `pipelines/node_types.py`

One `EXCLUDED_LAUNCHES` entry with a comment stating why: user-triggered from
the Results tab, no fixed port shape to express as a `PortSpec`. Same
reasoning as its neighbor `launch_annotation_export`.

### Frontend

`AnnotationFeatureTable.tsx` already computes `isGenBank` (line 197) and uses
it to *hide* the export control, because GenBank features are not
line-addressable. The extraction action therefore renders into a slot that is
empty today, for exactly the files this feature targets — no competing
control and no layout conflict.

`genbank_has_sequence` must be added to `types.ts`; it is currently absent.

## Testing

Backend unit tests for `genbank_sequence.py`:

- single record; multi-record `.gbff` (GS-2)
- gzipped and plain input (GS-6)
- all three accession tiers, and the `unknown` fallback (GS-3)
- **agreement with `genbank_reader` on the same fixture** (GS-4) — assert the
  two modules produce identical accessions, which is the test that catches
  D-3 drifting
- a record with no `ORIGIN` (GS-8)
- line-format correctness: counters stripped, 60-column wrap (GS-5)

Guard tests (GS-17, GS-19, GS-20): second launch does not queue; launch after
deleting the derived object does queue; launch after renaming it does not.

Classification: run the **entire** `TestExhaustiveness` class in
`tests/pipelines/test_node_types.py`, not only the test this change appears to
touch. CLAUDE.md records why: #355 landed a `NodeTypeSpec` and an exclusion
for the same launcher in two commits, satisfying the test named in the issue
while silently failing `test_no_launcher_is_both_used_and_excluded` in the
same class.

Manual verification in the running app, which no unit test above substitutes
for: extract from a real GenBank, then align against the resulting reference.
GS-15 is the requirement at risk here — a reference that no picker offers
would pass every unit test listed above. This is the "check it against the
real database" point from CLAUDE.md, where suggestion rules passed a green
suite while refusing to align a project with one usable reference.

## Scope and effort

The issue is labeled `difficulty: high`. The honest reason is breadth — seven
or eight files across the pipeline, queue, API, service, and frontend layers —
rather than difficulty in any one place. The streaming pass itself is the
simplest part of the change.

## Out of scope

- Extracting protein translations from `/translation` qualifiers. That is a
  different output (amino acids, one record per CDS) answering a different
  question, and nothing in #348 asks for it.
- A canvas node type for extraction. Tracked for its siblings by
  [#371](https://github.com/syntheticgio/bioflow/issues/371); this launcher
  joins them as an exclusion rather than pre-empting that design.
- Automatically extracting at ingest. Extraction is on demand by explicit
  design constraint in #348 — a 300MB `ORIGIN` block becomes a 300MB FASTA,
  and that cost should be user-initiated.
