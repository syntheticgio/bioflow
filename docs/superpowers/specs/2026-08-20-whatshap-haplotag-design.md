# WhatsHap haplotag read-level BAM haplotagging

Date: 2026-08-20.

Closes [#710](https://github.com/syntheticgio/bioflow/issues/710).

**Blocked by [#628](https://github.com/syntheticgio/bioflow/issues/628)** —
this design assumes #628 has landed the `whatshap` probe, the
`TOOL_META["whatshap"]` entry, the Dockerfile install, `settings.whatshap_path`,
`RunKind.PHASE_VARIANTS`, and the phase card's BAM picker. Nothing here
re-specifies any of that; see
`docs/superpowers/specs/2026-08-20-whatshap-variant-phasing-design.md`.

## Problem

#628 makes phase *queryable at the variant record* — which variants travel
together in a phase block. It does not answer the read-level question:
**which reads support which haplotype**. That question is what you need to

- inspect a locus in IGV with reads coloured and grouped by haplotype,
- feed haplotype-partitioned reads into downstream per-haplotype assembly or
  per-haplotype consensus,
- eyeball whether a phase block is supported by real read evidence or by two
  reads and a coin flip.

`whatshap haplotag` answers it: given a **phased** VCF (PS tags) and the BAM
those reads came from, it writes a new BAM in which every assignable read
carries `HP` (haplotype 1 or 2) and `PS` (phase block) tags. It does *not*
emit a VCF, so it cannot populate #628's `phase_set` column — which is
precisely why it was split out rather than folded into #628.

## What exists today

Verified against this worktree on 2026-08-20 (with #628 assumed landed):

- **`_apply_align_reads` in `queue/results.py:1440`** is the template for a
  job that produces a **new BAM object**: `ingest_local_file(...,
  role=ObjectRole.ALIGNMENT, derived_from=[parents], produced_by_job=...,
  metadata=dict(reads.metadata))`, then records outputs on the run and
  **chains an `index_bam` job** using the fresh BAM's digest. That chaining is
  not optional decoration — `_apply_index_bam` (`results.py:1640`) is what
  stamps `facts.has_index = True`, without which the Results tab and
  Provenance panel report a missing index forever.
- **`SidecarRole.BAI`** already exists; a haplotagged BAM needs no new sidecar
  role, only the existing `index_bam` chain.
- **`ObjectRole.ALIGNMENT`** already covers BAMs. No new `ObjectRole`.
- **`build_consensus_card`** (`suggestion_service.py:1248`) is the closest
  card analog: anchored on the BAM, gated on a tool probe plus one resolved
  companion object, UNAVAILABLE with a specific reason when either gate fails.
- **`running_now.ENDPOINT_JOB_TYPES`** maps card endpoint → job type; a
  missing entry means the Launch button never greys out while the job runs.
- **`provenance_walker._NO_NARRATIVE_STEP`** is a partition against the
  handler registry: every handler is either in it or has a narrative verb.

## Decision 1: anchor the card on the phased VCF, not the BAM

Both inputs are required, so either could anchor. **Anchor on the phased
VCF**, with the BAM chosen in the configure dialog — the mirror image of
#628's phase card (anchored on the *unphased* VCF, BAM picked in the dialog).

Why: a phased VCF is a scarce, unambiguous artifact — it exists only because
someone ran phasing, and its presence is the exact precondition haplotagging
has. A BAM, by contrast, is the *common* object in this app; anchoring there
would put a haplotag card on every alignment in every project, almost all of
which have no phased VCF, so the card would render UNAVAILABLE far more often
than not. Anchoring on the phased VCF means the card appears where it is
nearly always actionable.

Consequence the implementer must not lose: the card must recognise a phased
VCF *by evidence*, not by filename. **Gate on the VCF's `phase_set` column
being populated** — #628 already computes it for every VCF via
`run_vcf_stats`, so "this VCF has at least one non-NULL `phase_set`" is a
cheap, tool-agnostic, honest test. A VCF phased by something other than
WhatsHap qualifies, and a WhatsHap output that silently phased nothing does
not. Do **not** gate on the object name containing `whatshap`.

*Rejected:* gate on a completed `phase_variants` run in the project. That is
the provenance-inference mistake #628's Decision 2 already rejected in the
other direction, and it would miss an imported phased VCF.

## Decision 2: the output is a new BAM object, not tags merged onto the old one

`haplotag` writes a whole new BAM. Ingest it as a **new `DataObject`** with
`role=ObjectRole.ALIGNMENT`, `derived_from=[source_bam.id, phased_vcf.id]`,
carrying the source BAM's `metadata` forward (sample-level biology is
unchanged by tagging, same reasoning as `_apply_align_reads`).

Why not replace or mutate the source BAM: the untagged BAM is the input to
every other alignment-consuming card in the app, and haplotagging is lossy in
one direction the user cares about (reads WhatsHap could not assign are
retained but untagged — a real analytical distinction). Two objects, both
inspectable, both re-runnable, is the honest shape and matches how
`polish`/`consensus` treat their outputs.

`derived_from` carries **both** parents because both are biologically
meaningful — the phased VCF is not scaffolding, it is where the haplotypes
came from. Same call `_apply_align_reads` makes for reads + reference.

## Decision 3: chain `index_bam`, in the applier, after ingest

The haplotagged BAM is useless in IGV without a `.bai`, and `has_index` is
stamped only by `_apply_index_bam`. So `_apply_haplotag` must chain an
`index_bam` job exactly as `_apply_align_reads` does — **after** ingest, gated
on `bam.blob_sha256`, because the digest does not exist until the file is in.

This is called out as a decision rather than an implementation detail because
it is the single most likely thing to be omitted, and its failure mode is
silent: the job succeeds, the object appears, and the BAM is simply never
usable in a viewer.

## Decision 4: two facts on the output, and they must be honest

Store on the haplotagged BAM:

- `haplotagged_reads` — reads that received an `HP` tag,
- `haplotag_untagged_reads` — reads WhatsHap could not assign,
- `haplotag_phase_blocks` — distinct `PS` values written,
- `haplotag_source_vcf` — the phased VCF's id.

The first two matter because **a haplotag run that assigns nothing exits
zero**. A BAM where 99% of reads are untagged is a failed analysis wearing a
success badge, and the counts are the only thing that says so. Source them
from `--output-haplotag-list` (WhatsHap's per-read assignment TSV) rather than
by re-reading the BAM, and verify the exact column layout of that file against
a real run before writing the parser.

## Decision 5: no new tool registration, no new `PipelineType`

`haplotag` is a subcommand of the same binary #628 registers. It reuses
`tools.whatshap()`, `TOOL_META["whatshap"]`, and `settings.whatshap_path`
unchanged. The `usage` string in `TOOL_META` describes behaviour, not flags,
so it already covers both subcommands; if it reads as phasing-only after #628
lands, widen that one sentence rather than adding a second entry.

## Components

### `backend/app/pipelines/whatshap_runner.py` (extended, not new)

Pure functions beside #628's, testable without the binary:

- `build_haplotag_command(*, whatshap_path, reference, phased_vcf, bam, out,
  haplotag_list, ignore_read_groups=False, sample=None)` →
  `whatshap haplotag --output OUT --reference REF
  --output-haplotag-list LIST PHASED_VCF BAM` (+ `--ignore-read-groups`,
  `--sample`). Output is a flag, not positional — unlike `phase`, whose
  ordering #628's builder encodes; do not copy the positional shape across.
- `haplotagged_name(bam_name)` → `foo.haplotag.bam`, mirroring
  `phased_name`.
- `parse_haplotag_list(path) -> dict` — tagged / untagged counts and distinct
  phase blocks, per Decision 4. Unit-tested against a fixture captured from a
  real run.

`--reference` is required whenever the BAM lacks `MD` tags; passing it
unconditionally when the alignment target resolves is the safe default, and
the resolution seam is `reference_assembly.resolve_alignment_target_for_bam`
(already used by `build_consensus_card`).

### `backend/app/queue/variant_handlers.py`

`_haplotag(ctx)` — `@handler("haplotag", mode=SUBPROCESS, job_class=COMPUTE,
resources=JobResources(cpu=1, mem_mb=4096, io=IoClass.HEAVY),
max_attempts=2)`. Resolves the phased VCF, the BAM, and the reference; runs
the command; parses the haplotag list; returns `{object_id, bam_object_id,
output, facts, tool="whatshap", tool_version}`.

`mem_mb=4096` rather than `feature_coverage`'s 1024: haplotag holds the phase
blocks in memory while streaming the BAM, and the failure mode of guessing low
is an OOM-killed run that also poisons the timing models.

### `backend/app/queue/results.py`

`_apply_haplotag` — ingest per Decision 2, `record_outputs` on the run, chain
`index_bam` per Decision 3, and merge the Decision 4 facts onto the **new**
BAM using per-key `facts.<key>` paths, never a whole-dict merge (the #606
trap: `_apply_ingest_headers` runs on the same object at nearly the same
moment and a merge from a stale snapshot erases its header facts).

### `backend/app/services/pipeline_service.py`

`launch_haplotag(object_id, alignment_id, params, owner, ...)` — eligibility
check (VCF has populated `phase_set`; BAM resolves; reference resolves),
payload assembly, `enqueue`. Records both the VCF and the BAM as run inputs.

### `backend/app/api/v1/pipelines.py`

`POST /pipelines/haplotag` → `JobOut`, 201.

### `backend/app/pipelines/node_types.py`

`haplotag` `NodeTypeSpec`: inputs `variants` (phased VCF) + `alignment` (BAM),
output `alignment` (BAM), `run_tool="whatshap"`,
`run_kind=RunKind.PHASE_VARIANTS` (reusing #628's member — haplotagging is the
read-level half of the same operation, and a separate `RunKind` would split
one user-visible activity across two activity-feed verbs). Plus a
`_launch_haplotag` adapter.

### `backend/app/services/suggestion_service.py`

`build_haplotag_card(obj, alignments)` — kind `"haplotag"`, category
`"VARIANTS"` (reused, as #628 reuses it). Gates, each with its own UNAVAILABLE
reason: whatshap installed; `obj` is a VCF/BCF whose `phase_set` column is
populated (Decision 1); at least one BAM selectable. Registered in
`CARD_BUILDERS` **and** `_CONFIGURE_DIALOGS` (the BAM picker).

### `backend/app/services/running_now.py`

`ENDPOINT_JOB_TYPES["/pipelines/haplotag"] = frozenset({"haplotag"})`.

### `backend/app/services/provenance_walker.py`

`haplotag` gets a **narrative verb** ("haplotagged"), not a
`_NO_NARRATIVE_STEP` entry — it produces an object a person opens, which is
exactly the line that separates the two sides of that partition.

### Frontend

- `BamResults.tsx` shows the haplotag facts when present, with the untagged
  count beside the tagged one — never the tagged count alone (Decision 4).
- `metricInfo.METRIC_INFO` gains an entry per new `<Stat metric="...">`;
  without one the InfoMarker silently renders nothing (`metricInfo.test.ts`
  enforces it).

## Testing

Backend tests run from the worktree with `./backend/run-worktree-tests.sh`,
never `docker compose exec api` — the latter silently tests main's code.

- **Command construction** — `build_haplotag_command` argv: output is a flag,
  reference present, VCF before BAM, optional flags only when asked.
- **`parse_haplotag_list`** — tagged/untagged/phase-block counts from a
  fixture; a list where every read is untagged parses to
  `haplotagged_reads=0` rather than raising or reporting success.
- **Card, failing direction first** — UNAVAILABLE when the probe is patched
  off (patch `spec_for`, not `tools.whatshap`, which the registry captured at
  import), UNAVAILABLE when the VCF's `phase_set` is entirely NULL, and
  UNAVAILABLE when no BAM resolves. The image ships whatshap, so the
  "available" assertion proves nothing on its own.
- **`TestExhaustiveness`, the whole class** — `node_types` partition and the
  `_NO_NARRATIVE_STEP` partition are registry *pairs*; running only the one
  test a gap names is how #355's collision got through.
- **`test_every_endpoint_is_a_real_route`** — covers the new
  `ENDPOINT_JOB_TYPES` key.
- **Real-database check** — run haplotag on a real phased VCF (from a #628
  run) plus its source BAM; confirm the output BAM carries HP/PS read tags
  (`samtools view | grep HP:i:`), that a `.bai` was chained and
  `facts.has_index` is true, and that the tagged/untagged counts match the
  haplotag list. This is the check that catches a zero-assignment run
  reporting success.

## Verify before implementing (not asserted above)

1. **`--output-haplotag-list` column layout** on the installed WhatsHap
   version — Decision 4's parser depends on it, and it is not stable across
   the tool's history.
2. **Whether `--reference` is genuinely required** for BAMs this app produces
   (i.e. whether they carry `MD` tags). If they do, passing it is harmless;
   if they do not, omitting it is a silent failure.
3. **`--ignore-read-groups` necessity** for single-sample BAMs whose `@RG`
   sample name does not match the VCF's — the common cause of "0 reads
   tagged" with a zero exit code.
4. **Peak memory on a real human-scale BAM**, to confirm or correct the
   `mem_mb=4096` estimate before it reaches the timing models.

## Out of scope

- **Splitting the haplotagged BAM into per-haplotype BAMs**
  (`whatshap split`). A separate artifact-multiplying concern; the HP tags
  make it derivable later.
- **Rendering read-level haplotypes in the app.** The value here is a BAM the
  user opens in IGV; an in-app haplotype-coloured read viewer is its own
  issue.
- **Auto-running haplotag after phasing.** Like every other card in this app,
  it is deliberate and user-launched.
- **Re-registering WhatsHap.** Decision 5 — #628 owns the tool entry.
