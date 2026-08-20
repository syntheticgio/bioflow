# Metagenomics support — scoping design

Date: 2026-08-20.

Scopes [#630](https://github.com/syntheticgio/bioflow/issues/630),
"Evaluate metagenomics support (MEGAHIT/metaFlye + MetaBAT2 + CheckM2)".

#630 is an **epic and an evaluation**, not an implementation issue. What it
asks for is a design pass that settles the assembler choice, the binner
choice, and the data-model question *before* any child issue is scoped. This
document answers those three, and proposes the child issues that follow. It
deliberately does not specify command-line flags or file layouts for tools
whose child issue has not been cut yet — that is the child spec's job.

## Why this is a domain, not a parameter

Every assembler this app ships assumes **one organism per sample**. A
metagenomic sample breaks that assumption at the root: the assembly is a
mixture, contigs from different organisms are interleaved in one FASTA, and
the unit a biologist actually wants — a MAG, one organism's draft genome — does
not exist until a *binning* step separates them. So the workflow gains a stage
that has no analog anywhere in the current app:

```
reads → assembly (mixed contigs) → binning (N bins) → per-bin QC → N MAGs
```

The middle two stages are what make this an epic. Everything downstream of
binning (align, annotate, QC a MAG) is existing machinery pointed at a new
object.

## What exists today

Verified against this worktree on 2026-08-20:

- **`assembler_registry.FLYE_SPEC`** registers Flye with `mode_flags` keyed by
  `ReadChemistry`, a `layout`, an `AssemblyMemoryModel`, three `Output`s, and
  a `fields` tuple of `ParamField`s. **There is no `--meta` field and no
  `meta` key anywhere in `assembly_params.py` or `assembly_runner.py`** — so
  #630's "check whether it already supports `--meta`" resolves to **no**.
- **`Assembler`** (`assemblers.py:18`) has `FLYE`, `HIFIASM`, `SPADES`,
  `ABYSS`. `HIFIASM_SPEC` shows the precedent for a spec declared with
  `tool=None` so the API can say "not installed in this build".
- **`_apply_assemble_reads`** (`results.py:1547`) already ingests **two**
  `DataObject`s from one job — contigs (as `ObjectRole.REFERENCE`) and the
  graph (`ObjectRole.ASSEMBLY_GRAPH`) — with the contigs ingested first and
  independently so a secondary-output failure cannot lose the expensive one.
  **This matters more than anything else here**: N-objects-from-one-job is
  precedented, not new.
- **Kraken2 already ships** — probe, `TOOL_META`, a `classify_reads` job, and
  `kraken_db_registry.py`, which pins three pre-built databases by dated
  snapshot with a known-a-priori `mem_mb` (deliberately *not* fitted from the
  memory model, since a fit from unrelated jobs under-provisions into an OOM).
  So #630's "is Kraken2 a prerequisite" resolves to **already present**, and
  its database registry is the pattern CheckM2 needs.
- **compleasm** is the existing completeness scorer, with a launch-time
  database check rather than a probe-time one.
- **MEGAHIT, MetaBAT2, and CheckM2 are absent** from the codebase entirely.

## Decision M1: metaFlye first, MEGAHIT as a separate child issue

**Add `--meta` to the existing Flye spec.** Do not add a new assembler to get
long-read metagenome assembly.

metaFlye is not a different tool — it is a flag on the binary already
installed, already probed, already registered, already resource-modelled. The
work is one `ParamField` plus the flag in command construction, and it makes
the app metagenome-capable for long reads at a fraction of the cost of a new
tool registration (which per CLAUDE.md is six hand-maintained registries, four
of which fail silently).

MEGAHIT is the right tool for *short-read* metagenome assembly and remains
worth adding — but as its own child issue, competing on merit with everything
else in the backlog, not as a prerequisite for the domain. SPAdes already
ships and has a `--meta` mode of its own; **whether SPAdes `--meta` closes the
short-read gap well enough to defer MEGAHIT indefinitely is an open question
this spec does not settle** (see Open questions).

*Rejected:* adding MEGAHIT first because it is "the standard". It is the
standard for short-read metagenomics specifically, and it would deliver a
new-tool-registration cost before proving anyone here wants the domain at all.

**Caveat the child spec must resolve:** `--meta` changes Flye's memory
profile. `FLYE_SPEC.memory_model` assumes ~40 bytes per genome base against a
single genome size; for a community, "genome size" is not a meaningful input.
Do not reuse the single-genome model unexamined — an under-provisioned
estimate is an OOM, and an OOM-killed run also poisons the timing models.

## Decision M2: MetaBAT2, and binning is where the epic's real work is

MetaBAT2 is the binner. It is standard, actively maintained, and takes exactly
what this app can already produce: a contigs FASTA plus per-contig coverage
depth from a BAM of the reads aligned back to those contigs.

That last clause is the point. The align card already offers aligning reads
back against their own assembly (which is why `_apply_assemble_reads` gives
contigs `ObjectRole.REFERENCE`), and **#626's mosdepth work already computes
per-contig depth**. So the binning input is assemblable from parts that exist,
rather than needing a bespoke coverage path.

The binning child issue is the one that carries the data-model decision (M3)
and should be scoped first among the new-tool children.

## Decision M3: a bin is a first-class object; the bin set is not a new kind

The data-model question #630 raises — "a metagenomic assembly produces
*multiple* genome-like objects from one sample, which may not fit the current
one-assembly-per-object data model".

**It fits.** Each bin is ingested as its own `DataObject` with
`ObjectRole.REFERENCE` — the same role a single-organism assembly gets — with
`derived_from=[mixed_contigs.id, bam.id]` and a `bin_*` fact namespace. The
one-job-many-objects shape is precedented by `_apply_assemble_reads`; binning
differs only in that N is data-dependent rather than fixed at two.

Why this rather than a new `ObjectRole.MAG` or a container "bin set" object:

- A MAG **is** a draft genome. Everything a user wants to do next — align
  against it, annotate it, run completeness on it, view its GC tracks — is
  existing machinery that keys off `REFERENCE`. A new role would make each of
  those a per-role special case, and CLAUDE.md's registry warning is precisely
  about hand-maintained dicts keyed by an enum where a missing member is
  silently skipped. A new role means auditing every one of them.
- A container object would need its own viewer, its own results tab, and its
  own provenance semantics, to express something `derived_from` already
  expresses.

What genuinely does need care, and what the binning child spec must specify:

- **Ingest each bin independently**, in the `_apply_assemble_reads` posture:
  one bin failing to ingest must not lose the other forty.
- **N is unbounded.** A deep community can produce hundreds of bins. The child
  spec must state a cap (or an explicit decision not to cap) and what the UI
  does at that scale — a project view that lists 300 new objects from one
  click is a usability failure even when every object is correct.
- **Facts must carry bin identity** (`bin_index`, `bin_source_assembly`,
  `bin_contig_count`, `bin_total_bases`) so a bin is traceable to its
  community without a container object.
- **Unbinned contigs are a real result**, not a discard. MetaBAT2 leaves
  contigs it cannot place; whether they become an object or a fact is a child
  decision, but silently dropping them hides how much of the community was not
  recovered.

## Decision M4: CheckM2 is bin QC, and its database is the hard part

CheckM2 scores completeness and contamination per bin — the compleasm analog
for MAGs, and the thing that makes a bin trustworthy or not.

The tool is not the problem; **the database is**. CheckM2 needs a
multi-gigabyte pre-built DIAMOND database, which is the same shape of problem
`kraken_db_registry.py` already solved: pin a dated snapshot, record its size
and checksum, know the memory cost a priori, check availability at *launch*
time (compleasm's posture) rather than at probe time, and never let a
"latest" alias make a run unreproducible.

The CheckM2 child spec should follow `kraken_db_registry.py` closely and cite
it. A CheckM2 child issue that treats the database as an implementation detail
will rediscover every constraint that registry's docstring already records.

## Decision M5: Kraken2 labels bins; no new tool needed

#630 asks whether Kraken2 is a prerequisite or complement. **Complement, and
already present.** Running Kraken2 over a bin's contigs gives it a taxonomic
label ("this MAG is probably *Bacteroides*"), which is what makes a bin set
readable rather than forty anonymous FASTAs.

This needs no new tool — only the existing classify path pointed at contigs
rather than reads, which may or may not be a trivial change depending on how
`classify_reads` is bound to FASTQ input. **Verify that before scoping it**;
it is plausibly the cheapest genuinely useful item in this epic.

## Proposed child issues

Cut in this order. Each is independently mergeable and independently useful —
a property worth protecting, because it means the epic can stop after any one
of them without leaving a half-built domain.

| # | Child | Depends on | Why this order |
|---|---|---|---|
| 1 | **metaFlye: `--meta` on the existing Flye spec** | — | Cheapest real capability. One `ParamField` + flag + a revisited memory model. Delivers long-read metagenome assembly with no new tool. |
| 2 | **MetaBAT2 binning** | 1 (for something to bin) | The core of the epic and the carrier of M3's data-model decisions. |
| 3 | **CheckM2 bin QC** | 2 (needs bins) | Makes bins trustworthy. Carries the database-registry work (M4). |
| 4 | **Kraken2 labelling of bins** | 2 | Possibly trivial; verify the classify path's input binding first (M5). |
| 5 | **MEGAHIT short-read metagenome assembly** | — | Independent. **Justified** — #731 measured metaSPAdes failing under a memory cap that MEGAHIT completes within (see Open questions). |

Child 1 alone leaves the app able to assemble a community but not separate it,
which is honest and useful (a metagenome assembly is a legitimate artifact).
Children 1–2 deliver MAGs. Children 1–3 deliver MAGs anyone should trust.

## Open questions (for the child specs, not settled here)

1. ~~**Does SPAdes `--meta` cover the short-read case?**~~ **SETTLED (#731,
   2026-08-20): partly — it ships, and MEGAHIT is still justified.** SPAdes
   `--meta` was added as a fourth `mode` choice, and the bake-off then ran
   against it. Quality was a wash (MEGAHIT's N50 8% higher on 3% less
   assembly) and MEGAHIT was 2.8x faster, neither of which decided it. What
   decided it was memory under a cap: on the same community, metaSPAdes
   terminated with `Cannot allocate memory` and produced nothing, while
   MEGAHIT completed within a 500 MB budget and produced an assembly identical
   to its uncapped one. So **child 5 is worth cutting**. Full numbers in
   `2026-08-20-short-read-metagenome-assembly-design.md`.
2. **What replaces the single-genome memory model for metaFlye?** Per M1, the
   `bytes_per_genome_base` model has no meaningful input for a community.
3. **What is the bin-count cap, and what does the UI do at it?** Per M3.
4. **Is `classify_reads` bindable to contigs?** Per M5, decides whether child
   4 is trivial or not.
5. **Does CheckM2 have a linux-aarch64 path?** Per CLAUDE.md's standing rule,
   check bioconda's `linux-aarch64` subdir before believing a GitHub release
   binary is the only option — that is the difference between the tool working
   on Apple Silicon and an arm64 skip.

## Out of scope for this epic

- **Strain-level resolution** within a bin (inStrain and similar). A different
  question from "which organisms are here".
- **Metatranscriptomics.** The RNA-seq stack is its own domain.
- **Comparative/differential abundance across multiple communities.** Needs a
  multi-sample model this app does not have.
- **Automatic MAG submission** to public archives.
