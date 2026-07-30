# Actions tab: pipeline suggestions

A rebuilt Actions tab in three sections: **Computations** (the existing
tool-picker flows), **Launch a pipeline on this file** (a grid of pre-answered
suggestion cards chosen by a backend rule engine), and **Manage this file**
(today's tags/role/pair/delete content in a two-column layout).

## Problem

The Actions tab is a flat stack of five unrelated controls -- download, tags,
role conversion, pair editing, delete -- with no ordering principle beyond the
sequence they were added in. Meanwhile the operations a user actually comes to
this panel to perform (trim, align, call variants, run QC) are not in the
Actions tab at all: they sit as five buttons crowded into the file headline,
where they push the file's identity to one side.

Separately, every pipeline launch currently starts from a blank tool picker.
Choosing between minimap2 and bwa-mem2 requires knowing that the answer is
determined by read chemistry, and then knowing what this file's chemistry is.
The app already infers that (`pipelines/qc_stats.py`) and then declines to act
on it: the inference is used to preselect a preset *inside* the align dialog,
after the user has already made the choice the inference could have made for
them.

## What already exists

Worth stating precisely, because it decides what is a rule and what is a guess.

**Chemistry inference** (`pipelines/qc_stats.py:30`, `infer_chemistry`) takes
platform + mean read length + mean quality and returns a `ReadChemistry` plus
*a human-readable reason string*. The vocabulary is `SHORT`, `ONT_SIMPLEX`,
`ONT_DUPLEX`, `HIFI`, `CLR`, `UNKNOWN`. It is deliberately conservative:
ambiguous evidence returns `UNKNOWN` rather than a guess presented as fact.
Stored on the object as `facts.qc_read_chemistry`, with the reason at
`facts.qc_read_chemistry_reason`.

**Preset mapping** (`pipelines/align_runner.py:94`, `preset_for_chemistry()`)
already maps chemistry to a minimap2 preset (`map-ont`, `map-hifi`, `lr:hq`).
Note its documented caveat: `UNKNOWN` falls back to short-read, so callers with
a platform to fall back on should prefer `suggested_preset` -- "unknown
chemistry on a PacBio file" should stay `map-pb`, not become `sr`. The align
rule below gates on `UNKNOWN` before either is consulted, so this fallback is
not reached from a card; it matters only if that gate is later relaxed.

**Tool probes** (`pipelines/tools.py`) resolve paths and versions, cached. The
installed set is: fastp, fastqc, cutadapt, trimmomatic, bwa-mem2, minimap2,
bowtie2, hisat2, samtools, bcftools, clair3, nanoplot. **No assembler** --
no Flye, no SPAdes, no Canu.

**Assay vocabulary** (`metadata/schemas.py:111`): `WGS`, `WES`, `RNA-seq`,
`scRNA-seq`, `ATAC-seq`, `ChIP-seq`, and others, populated automatically from
SRA's `LIBRARY_STRATEGY`.

**Launch endpoints** already exist and work: `POST /pipelines/trim`,
`/pipelines/qc`, `/pipelines/align`, `/pipelines/variants`. This design adds no
new execution path -- an `available` card posts to the same endpoint the dialog
posts to, with parameters filled in.

### Measured against the live database

57 objects, and the numbers decide the design more than the ideal case does:

- **10 FASTQs, but QC has run on only 4 objects.** `qc_read_chemistry` is
  absent on 52 of 57. So a FASTQ with unknown chemistry is not an edge case,
  it is *the common case*, and the gated-card path is the default view rather
  than a rare fallback.
- **4 references** across the projects, so the multi-reference tiebreak is
  live rather than hypothetical.
- **Organism is set on 20 objects** and spans the full range: bacteria
  (*E. coli*), fungus (*S. cerevisiae*), protozoan (*T. brucei*), plant
  (*Lycoris aurea*).
- **Assay is set on 13 objects: 12 WGS and 1 ChIP-seq. Zero RNA-seq.** The
  splice-aware rule below is implementable and correct, but it would not fire
  on any file currently in the database. It is specified for correctness, not
  because it will be visible.

## Design

### The card

Four card kinds, one per type, in fixed order: `preprocess`, `align`,
`variants`, `assemble`. Fixed order matters -- a card's position never moves
between files, so the grid stays readable when switching selections.

```
kind         preprocess | align | variants | assemble
category     "ALIGN"                       -- small-caps label
title        "minimap2 map-ont -> BAM"
description  "Align to GCF_000002445.2 (ASM244v1), sort and index."
why          "ONT simplex reads at 12.4 kb -- map-ont preset."
status       available | unavailable
reason       null, or "Run QC to determine read chemistry."
launch       null, or {endpoint, params}
```

`why` is the load-bearing field: it carries `infer_chemistry`'s existing reason
string, so a card explains *why these choices for this file* rather than
asserting them. This follows the precedent that module already sets -- a label
and its justification, never a bare verdict.

`launch` being null and `status` being `unavailable` always agree. The frontend
renders a disabled button whenever `launch` is null, so the two cannot drift
into a card that looks clickable and does nothing.

### Card status: two states, not three

A card is `available` (inputs known, tool installed, runnable today) or
`unavailable` (button disabled, with a reason naming which of three causes
applies: a missing prerequisite, a missing tool, or no pipeline system yet).

**Explicitly rejected:** a third state where clicking a gated card runs its
missing prerequisite. That is partial-DAG behavior which a real pipeline system
would replace, so building it now is throwaway work. When DAGs land, the
gated-prerequisite subset graduates to `available` without the card layout
changing.

### Rules

**Preprocess** -- fastq only. fastp for short reads; for long reads, length and
quality filtering rather than adapter trimming. Available whenever the tool is
installed. Chemistry-unknown still yields a runnable card: fastp's defaults are
safe on either read type, so this card is not gated on QC.

**Align** -- fastq only.

- Tool: minimap2 with `preset_for_chemistry()`'s preset for long reads;
  bwa-mem2 for short; **hisat2 when assay is RNA-seq and the organism is
  eukaryotic** (splice-aware), falling back to bwa-mem2 for bacterial RNA-seq
  -- bacteria have no introns, so splice-awareness is wrong there. Organism
  determines the eukaryote/bacterium split.

  **How that split is decided:** `organism` is free text, so this is a lookup
  against a small hand-maintained genus table in the service (*Escherichia*,
  *Bacillus*, *Staphylococcus*, ... -> prokaryote; *Homo*, *Mus*,
  *Saccharomyces*, *Arabidopsis*, ... -> eukaryote), matched on the first word
  of the name, case-insensitively. **An unrecognised genus is treated as
  eukaryotic**, since splice-aware alignment on a genome without introns
  degrades gracefully -- hisat2 simply finds no junctions -- whereas the
  reverse loses real junctions silently. No taxonomy service call: this is a
  render-path decision and must stay local.

  The four organisms in the live database (*E. coli*, *S. cerevisiae*,
  *T. brucei*, *Lycoris aurea*) all resolve correctly under such a table, and
  it is the same table a future ploidy-aware rule would want.
- Unavailable when chemistry is `UNKNOWN`, reason "Run QC to determine read
  chemistry."
- Reference resolution, in this order (independent of the chemistry gate; see
  "combining the two gates" below):
  1. **Exactly one uploaded reference** -> use it, launchable.
  2. **Zero or 2+ uploaded, and the file names an organism** -> name the
     species, **unavailable**, "Fetching a reference genome for
     *S. cerevisiae* is not wired up yet."
  3. **2+ uploaded, no organism** -> pick deterministically (sorted by id,
     first), launchable, named in the card.
  4. **None uploaded, no organism** -> unavailable, "Upload a reference to
     align."

  Order is load-bearing: rule 1 claims the exactly-one case *before* metadata
  is consulted. Reversing that would make the card refuse a perfectly good
  local reference in favor of an unfetchable one -- worse behavior on a
  better-configured project.

  **Rule 2 names a species, not an accession.** `assembly_accession` is a field
  on reference and sequence-set files, not on a FASTQ of reads, so on the reads
  file where this card renders there is no official accession in metadata to
  name. Only `organism` is there. Going from organism to accession needs the
  NCBI resolver, and `assembly.lookup` (`metadata/assembly.py`) takes an
  accession as input -- there is no organism -> accession lookup in the
  codebase today.

  Adding one is explicitly out of scope: it would put a synchronous network
  call inside the suggestions endpoint, making every Actions tab open wait on
  NCBI in order to populate a card that is disabled regardless. The card says
  which species it would fetch for; naming the exact assembly is work for
  whenever fetching is actually built, at which point the resolver call belongs
  behind the launch, not behind the render.

  Rule 3's pick is deterministic, not random: the same card on every render and
  reload. A genuinely random choice would name a different reference each time
  the file is opened, which reads as a bug even though the choice is arbitrary.

- **Combining the two gates.** Chemistry and reference are independent, so both
  can block at once -- the common case on a fresh FASTQ in an empty project.
  The card is `available` only when both pass. When both fail, the reason names
  **the reference problem first**, because it is the one the user can act on
  without waiting for a job: "Upload a reference to align. Run QC to determine
  read chemistry." Two sentences, action-first.

  Tool availability is a third, rarer gate: if the selected aligner is not
  installed, that reason wins outright, since neither of the others is fixable
  while the tool is missing.

**Variants** -- bam only. Clair3 for long-read BAMs, bcftools for short. Reads
the platform from the BAM's own `@RG PL:` tag, which `align_runner` already
writes, rather than looking up the source FASTQ. When the tag is absent (a BAM
ingested from outside this app), unavailable with "Unknown sequencing
platform."

**Assemble** -- fastq only. Always `unavailable`. The reason names the honest
cause -- no assembler is installed -- rather than blaming the missing pipeline
system, which is true but not the blocking constraint.

### What organism does and does not do

Organism affects the align card's reference resolution and the splice-aware
branch, and it contributes to `why` text. **It does not otherwise select
tools.** Against the installed toolset, read chemistry decides nearly
everything: minimap2-vs-bwa-mem2 is long-vs-short, Clair3-vs-bcftools is
long-vs-short, and organism moves neither. Specifying organism-driven tool
choice beyond the splice rule would promise discrimination the installed tools
cannot cash.

Ploidy (haploid bacteria vs diploid mammal) is a parameter *within* the
variants card, not a different card, and is out of scope here.

## Layout

Three sections, top to bottom, in the Actions tab.

**Computations** -- the buttons moved down out of the file headline:
Preprocess, Align, Call variants, Run QC, Re-ingest. Each opens the tool picker
it opens today (`PipelineToolSelector` -> parameter dialog). Wiring is
unchanged; only location and the Trim -> Preprocess label change.

A computation is *the same operation with the picker in front of it*. A card is
a pre-answered instance: same `/pipelines/trim` call, different amount of
decision handed to the user. This is why Preprocess appears in both places and
QC appears only here -- QC takes no parameters, so a card version would be
identical to the button. The rule for what earns a card: **an operation earns a
card when it has choices worth defaulting.**

Computations sits *above* the grid so a card's "run QC first" reason points
upward at something visible.

**Launch a pipeline on this file** -- the card grid. Four columns on a wide
panel, collapsing to two then one. Category in small caps, title, description,
the why/reason line in muted text, then the button. The first available card
gets the filled primary button; the rest are outlined.

**Manage this file** -- two columns, small-caps label left, control right.
Download, Tags, Role on the left; Paired end, Delete on the right. Same content
as today (`TagEditor`, `RoleConverter`, `PairEditor`, download, delete); only
the arrangement changes, so those components are reused as-is.

### Naming: Trim -> Preprocess

UI only. The API route (`/pipelines/trim`), the job kind, and the `trim_*`
facts keep their names -- renaming those is a data migration for a cosmetic
gain, and the facts are already written onto existing objects. `TrimDialog`
keeps its filename; its heading becomes "Preprocess."

The new name is also the more accurate one: the operation already does adapter
trimming *and* length/quality filtering, which "Trim" undersells.

### Consequence: QC leaves the headline

Moving the computation buttons out of the file headline means Run QC is no
longer visible from the Quality tab -- where noticing that QC never ran is most
likely. A comment in the current code records that the headline placement was
deliberate for exactly this reason.

Replacement: **the Quality tab's empty state carries its own Run QC button**
when there are no QC facts. That is a more direct affordance than a persistent
headline button -- it appears exactly where the absence is noticed. Given that
QC has run on only 4 of 57 objects, this empty state is a frequently-seen
screen and worth making actionable.

## Data flow

`GET /api/v1/pipelines/suggestions/{object_id}` returns the ordered card list.
New service: `backend/app/services/suggestion_service.py`.

The frontend issues one query, `["suggestions", objectId]`, **only when the
Actions tab is open** -- a card grid nobody is looking at should not cost a
request per file selection. It invalidates on the same events that already
invalidate the object: QC finishing, a trim completing, a role conversion.
Those are precisely the events that change a card's status, and a stale grid
would say "run QC first" beside a file that just finished QC.

Each card kind is a function from the input tuple to a card or `None`; the
endpoint runs them in fixed order. Inputs: format kind, read chemistry, assay,
organism, and the project's references.

## Failure modes

- **Endpoint errors** -> the section renders empty with a one-line note, not an
  error box. Suggestions are advisory; a failed advisory should not look like a
  broken file.
- **Tool probe fails** -> that card is `unavailable` with the tool named. Probes
  are already cached in `tools.py`, so building four cards costs no subprocess
  work per request.
- **Object not found** -> 404, handled by the existing error middleware.

## Testing

`backend/tests/services/test_suggestion_service.py`, table-driven over
chemistry x format x reference-count x assay. Cases worth pinning:

- The four align-reference branches, especially that exactly-one-uploaded beats
  a known species reference.
- `UNKNOWN` chemistry gating align and variants, but *not* preprocess.
- RNA-seq selecting hisat2 on a eukaryote and bwa-mem2 on a bacterium, plus an
  unrecognised genus falling to the eukaryote default.
- Both gates failing at once yielding reference-first reason text, and a
  missing aligner overriding both.
- Deterministic selection when multiple references tie -- the regression test
  for "sorted by id, first". A random pick passes a single run and fails the
  suite intermittently.
- A BAM with no `@RG PL:` tag yielding an unavailable variants card.

Frontend gets no tests, per CLAUDE.md: verification is the browser at
localhost:5173.

## CLAUDE.md addition

A note near the tool guidance: adding a tool to `app/pipelines/tools.py` is
only half the change. `suggestion_service.py` decides which tool each card
recommends, so a new tool either needs a rule that can pick it or it will never
be suggested regardless of being installed.

The failure mode is silent, which is why it needs writing down: installing Flye
does not make the Assemble card light up: it leaves a card reading "no
assembler installed" next to an installed assembler.

## Out of scope

- Any DAG or multi-step pipeline execution. Cards launch single operations.
- Wiring up assembly, reference fetching, or Clair3 model selection.
- An organism -> assembly-accession lookup. See rule 2 above: it does not exist
  in the codebase, and adding it would put a network call behind every Actions
  tab render.
- Ploidy-aware variant parameters.
- Renaming the trim API route, job kind, or facts.
