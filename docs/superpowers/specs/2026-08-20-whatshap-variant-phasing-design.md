# WhatsHap read-based variant phasing

Date: 2026-08-20.

Closes #628.

## Problem

Once small variants are called (Clair3 / DeepVariant / bcftools), there is no
way to determine which variants are inherited together on the same chromosome
copy — phasing. This matters for compound-heterozygosity analysis,
haplotype-resolved interpretation, and any downstream use where knowing *which
allele combination* sits on one physical chromosome (rather than just the
unordered genotype) is required. WhatsHap is the standard read-based phasing
tool: given existing long-read (or linked / short-read) alignments plus a
called-variant VCF, it assigns phase. It is the natural next step after
Clair3, which already runs primarily against long-read data where read-based
phasing is most effective.

## Decision 1: record-level phase, not file-level

The phased VCF is stored as its own object — like `annotate_variants`
produces an annotated VCF — but phase must also be **queryable in the variant
table**, not merely present in the file. So the phase-set identifier becomes a
column, not just a tag inside the `.vcf.gz`.

Why record-level, given the issue's conservative "stored as a file" framing:
the entire point of phasing is to answer "which variants travel together on one
chromosome copy" — a question asked *across the table* (filter to a phase
block, read the haplotype top-to-bottom), not by eyeballing a VCF. File-level
phasing would leave that question unanswerable in the UI, which is the
capability this issue exists to add.

How it lands cleanly: the variant table (`variant_db.py`) is built by
`run_vcf_stats` from `vcf_stats_runner.QUERY_FORMAT` (the `bcftools query`
format string). Extending that format to pull `%PS` and adding a
`phase_set INTEGER` column makes phase appear for **any** VCF carrying PS tags
— WhatsHap's output, or any other phaser — with no per-tool branching. For an
unphased VCF the field is `.` → NULL, so the column is inert everywhere it is
not populated.

This is additive and global: every VCF's table gains `phase_set`. That is
intended, not a side effect to fence off.

Rejected alternative — file-level only: meets the issue's three success
criteria but not its stated purpose; phase would be invisible in the UI and
the variant table would be no more useful for compound-heterozygosity than
before.

## Decision 2: the BAM is chosen explicitly, not inferred

The phasing card operates on a VCF object, but WhatsHap needs the source BAM.
The card gets a **configure dialog** (like the annotate card's tool picker)
that offers a BAM selector; the launch body carries both `object_id` and
`alignment_id`.

Why not resolve the BAM from provenance (the producing `call_variants` run):
provenance resolution would silently pick "the" BAM when several exist for a
project, and a wrong pick produces a plausibly-phased-but-wrong VCF with no
error. Making the choice explicit is the honest default for read-based
phasing, where alignment quality is the dominant factor in phase accuracy.

Why not graph-node-only: the card is the primary surface — a one-click
shortcut from the file view — and success criterion 3 is about the card gating
correctly. A `phase_variants` node type is *still* added to `node_types.py`
for graph wiring (per the issue's original wording), taking a wired
`alignment` port; both the card and the node share `launch_phase_variants`.

## Decision 3: two modes — phase and polyphase; haplotag deferred

`whatshap phase` (single-sample) and `whatshap polyphase` (multi-sample) both
emit a **phased VCF** carrying PS tags, which is exactly what `phase_set`
consumes. They share the card, the runner, and the node type, differing only
in command construction and how many BAMs they take.

`whatshap haplotag` is **out of scope for this issue**. It emits a
haplotagged **BAM** (read-level HP/PS tags), not a phased VCF, so it cannot
populate `phase_set` — including it here would either force a second,
unrelated artifact type into the same change or leave its output disconnected
from the variant table. Filed as a separate follow-up issue (see below).

## Decision 4: license and citation, verified

WhatsHap is MIT-licensed. GitHub's license detector reports `NOASSERTION` for
it (unconventional copyright lines), exactly as it does for Sniffles — so
`TOOL_META["whatshap"]["license"]` records **MIT** only after verifying the
repo's LICENSE file, per CLAUDE.md's standing rule ("verify a license against
the project's own repository rather than recall it"). Citation: Martin,
Yoshimura et al., *Nature Computational Science* 2021
(https://doi.org/10.1038/s43588-021-00079-x). Homepage:
https://whatshap.readthedocs.io/ ; repository:
https://github.com/whatshap/whatshap.

## Decision 5: delivery and the arm64 question

WhatsHap is a Python package (`pip install whatshap`), depending on pysam,
numpy, scipy, and networkx. pysam, numpy, and scipy publish linux-aarch64
wheels; networkx is pure-Python. So a bare `pip install whatshap` is expected
to work on both architectures — **but this must be verified against PyPI at
build time**, following Sniffles' Decision 4: if any dependency lacks an
aarch64 wheel, add a builder stage (mirror the `edlib-build` pattern in
`backend/Dockerfile`) rather than letting the image fail only on Apple
Silicon, where the naive install passes x86-64 CI and breaks silently.

Rejected: bioconda — `Dockerfile`'s own comment states the repo's position
(Debian, not conda, to avoid carrying a conda install for a handful of tools).

## Components

### `backend/app/config.py`

`whatshap_path: str = "whatshap"`, alongside `clair3_path` / `sniffles_path`.

### `backend/Dockerfile`

Install whatshap via pip (builder stage only if a dependency lacks an aarch64
wheel — verify per Decision 5). No new `SidecarRole`: the phased VCF uses the
existing TBI sidecar like every other VCF.

### `backend/app/pipelines/tools.py`

- `whatshap()` probe: `_probe("whatshap", settings.whatshap_path,
  ["--version"])`, added to the all-tools list and the `cache_clear` block at
  the bottom of the module.
- `TOOL_META["whatshap"]` carrying `homepage`, `repository`, `citation`,
  `citation_url`, `license` (MIT, verified per Decision 4), and `usage` (behaviour,
  not flags — flags change when the runner is tuned). This is what
  `test_every_tool_is_documented` requires (success criterion 1) and what the
  card reads for its reason text.

### `backend/app/pipelines/whatshap_runner.py` (new)

Pure functions over strings and paths, mirroring `csq_runner.py` so command
construction and error classification are testable without the queue or
filesystem:

- `WhatshapParams` — `mode` (`"phase"` | `"polyphase"`), `threads`, optional
  `sample`, `ignore_read_groups`, `distrust_genotypes`, `indels`.
  `as_dict` / `from_dict` with validation.
- `build_whatshap_phase_command(*, whatshap_path, reference, bam, vcf, out)`
  → `whatshap phase --reference REF --output OUT VCF BAM`.
- `build_whatshap_polyphase_command(...)` → `whatshap polyphase` with the
  sample→BAM mapping (one BAM per sample). Polyphase takes a single VCF plus
  one BAM per sample.
- `phased_name(vcf_name)` → `foo.whatshap.phase.vcf.gz` (or `.polyphase.`),
  mirroring `csq_runner.annotated_name`.
- A stderr classifier (benign vs real) in the
  `csq_runner.is_benign_gff_warning` shape. **The exact flag set is a
  `PHASE_MODE`-class decision settled by running against a real Clair3 VCF**,
  not chosen from the manual — recorded here so the tuning is a named constant,
  not scattered argv.

### `backend/app/models/run.py`

Add `PHASE_VARIANTS = "phase_variants"` to `RunKind`, with the display /
grouping comment the other members carry (separate from `VARIANT_CALLING`
because an activity line reading "phased variants" is not the same as "called
variants").

### `backend/app/pipelines/node_types.py`

- `phase_variants` NodeTypeSpec: `run_kind=RunKind.PHASE_VARIANTS`, inputs
  `variants` (VCF) + `alignment` (BAM), output `phased` (VCF).
  `launch=_launch_phase_variants`.
- `_launch_phase_variants` → `pipeline_service.launch_phase_variants(...)`.
- `NODE_TYPES` / `EXCLUDED_LAUNCHES` is a **partition** (CLAUDE.md's registry
  warning, #355): add the launcher to the right side and run the whole
  `TestExhaustiveness` class.
- Note: for polyphase the BAM input is per-sample. The single-port
  `alignment` suffices for `phase`; polyphase's multiple BAMs travel via
  `params` (the established "multi port smuggles through params" pattern, per
  `PortSpec`'s own comment), or a `multiple=True` port if that proves cleaner.
  Decided at implementation against a real multi-sample set.

### `backend/app/services/pipeline_service.py`

`launch_phase_variants(object_id, alignment_id, mode, params, owner, ...)`
enqueues the `phase_variants` run, recording `alignment_id` (and the VCF's
`object_id`) as inputs.

### `backend/app/queue/variant_handlers.py`

`_phase_variants(ctx)` handler: run the whatshap command (phase / polyphase
per `mode`), index the output VCF (`_index_vcf`), return
`{ object_id, output, index, tool="whatshap", tool_version, ... }`. **No DB
build here** — consistent with `annotate_variants`, the new phased object's
`run_vcf_stats` builds its table from the extended `QUERY_FORMAT`, so
`phase_set` is populated automatically.

### `backend/app/pipelines/vcf_stats_runner.py`

Extend `QUERY_FORMAT` to pull `%PS` (appended after the GT sample block; a
single first-sample value, `-u` makes it `.` when absent), and add
`"phase_set"` to `VARIANT_COLUMNS`. Existing assertions (`endswith("\n")`,
`"[\t%DP]" in`, `"%INFO/DP" not in`) still hold.

### `backend/app/pipelines/variant_db.py`

- `CREATE TABLE variants` gains `phase_set INTEGER` (placed beside `gt`).
- `build_variant_db` parses the trailing `%PS` field → `int` (None for `.`);
  inserts it in the matching column position.
- `VariantFilters` gains `phase_set: int | None`.
- `query_variants` / `count_variants` carry `phase_set` in SELECT and the
  optional `WHERE phase_set = ?` clause (NULL means "any", matching the
  existing filter convention).

### Frontend

- `VariantTable.tsx`: a `phase_set` column (blank / `—` when NULL).
- Variant filter UI: a "phase block" input bound to `VariantFilters.phase_set`.
- (Optional, not required) sort / group by phase block to read a haplotype
  top-to-bottom.

### `backend/app/services/suggestion_service.py`

- `build_phase_card(obj, inputs)`: VCF / BCF only (like annotate); gated on
  `tools.whatshap().available` **and** prior variant calling on the object's
  project (a completed `call_variants` run, or the VCF is a `VARIANTS` output),
  **and** at least one selectable BAM existing. Returns UNAVAILABLE with the
  specific reason when any gate fails.
- kind `"phase_variants"`, category `"VARIANTS"` (reused — phasing is a variant
  operation; a new category is unnecessary surface).
- Registered in `CARD_BUILDERS` and `_CONFIGURE_DIALOGS` with a BAM picker (+
  mode selector: phase / polyphase). The configure dialog is what makes the
  explicit-BAM choice (Decision 2) real.

## Testing

Backend tests run from a worktree with `./backend/run-worktree-tests.sh`, never
the main-checkout exec form — the latter tests main's code from a worktree with
no error to say so.

- **Command construction** — `build_whatshap_phase_command` and
  `build_whatshap_polyphase_command` argv for default and non-default params;
  output is last; reference / inputs placed correctly.
- **Tool probe + metadata** — `whatshap()` available after install;
  `TOOL_META["whatshap"]` satisfies `test_every_tool_is_documented` (success
  criterion 1).
- **Card availability, failing direction** — the image ships whatshap, so an
  "available" assertion passes whether or not its patch worked; the
  load-bearing assertion is UNAVAILABLE when the probe is patched off, and
  UNAVAILABLE when no prior variant calling exists (CLAUDE.md's trap, success
  criterion 3).
- **QUERY_FORMAT** — still ends with `"\n"`, contains `%PS`, existing
  assertions intact; `VARIANT_COLUMNS` length matches the new field count.
- **Phase parsing** — `build_variant_db` populates `phase_set` from a row
  carrying `%PS`, and leaves it NULL when the field is `.` (unphased VCF);
  `query_variants` / `count_variants` honor the `phase_set` filter.
- **`TestExhaustiveness`, whole class** — node_types partition.
- **Real-database check (CLAUDE.md)** — run phasing on a real Clair3 VCF + its
  source BAM; confirm the phased VCF carries PS tags and that `run_vcf_stats`
  on it produces a table whose `phase_set` column is populated and filterable.
  This is success criterion 2 / 4 and the check that would catch a
  silently-unphased run.

## Success criteria

Restating #628's, with where each is satisfied:

1. **WhatsHap installs and passes `test_every_tool_is_documented`** —
   Decision 5 + the `tools.py` component.
2. **Phasing runs end-to-end against a Clair3 / DeepVariant VCF + source BAM,
   producing a phased VCF with PS tags** — `whatshap_runner.py` +
   `variant_handlers.py`, verified on real data.
3. **Suggestion card correctly gates on prior variant-calling completion** —
   `build_phase_card`, failing-direction tested.
4. **Phase is queryable, not just stored** — Decision 1: `phase_set` column,
   populated from `%PS` for any phased VCF, with a phase-block filter in the
   UI.

Out of scope, filed separately:

5. **`whatshap haplotag`** — read-level BAM haplotagging — new issue, blocked
   by this one (reuses the WhatsHap tool registration and card, emits a tagged
   BAM instead of a phased VCF).

## Follow-up issue: `whatshap haplotag`

`haplotag` assigns reads to haplotypes, writing a BAM with HP / PS read tags.
It does not produce a phased VCF and therefore cannot populate `phase_set`.
When built, it reuses the `whatshap` tool probe + `TOOL_META` entry and the
phase card's BAM picker, but emits a haplotagged `BAM` object (a distinct
artifact from the phased VCF this issue produces) for downstream assembly and
IGV inspection. Tracked as its own issue so this one stays coherent: every
mode here feeds the `phase_set` column.
