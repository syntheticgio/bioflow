# modkit ONT modified-base summarization — design

Date: 2026-08-20.

Closes [#631](https://github.com/syntheticgio/bioflow/issues/631).

This adds **per-site methylation calling** from the MM/ML tags ONT
basecallers write into a BAM. The app already has the whole prerequisite
stack — minimap2/winnowmap alignment, Flye, Clair3, NanoPlot — and answers
genetic questions well; this answers an **epigenetic** one from the same
BAMs, with no new sequencing and no bisulfite prep.

The design's centre of gravity is not the tool. It is success criterion 3:
**a BAM without modification tags must be told so, clearly, rather than
producing an empty bedMethyl and calling it success.** Everything below is
shaped by that.

## What exists today

Verified against this worktree on 2026-08-20:

- **`parsers._parse_alignment`** (`storage/parsers.py:97`) records
  `sort_order`, `reference_count`, `reference_names`, `reference_lengths`,
  `read_group_count`, `sample_names`, `platforms`, `program_chain`, and
  `has_index` — all from `pysam` on the **header only**. The module docstring
  is explicit and load-bearing: *"Everything here reads headers and a small
  prefix, never the whole file"*, because a 100 GB BAM has its metadata in the
  first few kilobytes and scanning the body would take minutes.
- **`program_chain`** captures `@PG` `PN`/`ID` in first-use order, truncated
  to 10. Dorado appears here on a Dorado-basecalled BAM.
- **`mosdepth_runner.py` landed** (#626), along with the `coverage` job,
  `config.coverage_dir`, `_apply_coverage`, and the per-contig track
  rendering. Its windowed-track shape is the model #631 anticipated, and it
  is now real rather than hypothetical.
- **`build_coverage_card`** (`suggestion_service.py:2321`) is the closest card
  analog: anchored on a completed BAM, read-only, one report artifact.
- **`_NO_NARRATIVE_STEP`** classifies `coverage` as non-narrative (statistics
  written back onto an existing object). modkit is **not** that shape — it
  produces a bedMethyl artifact a person opens.
- **modkit is absent** from the codebase entirely.

## Decision K1: MM/ML presence is a *record* fact, and the header cannot answer it

This is the design's hard constraint and the reason criterion 3 is not free.

MM and ML are **per-alignment-record tags**, not header fields. There is no
`@HD`/`@RG`/`@PG` field that states "this BAM carries base modifications". So
the existing `_parse_alignment` — headers only, by deliberate policy — cannot
detect them, and **extending it to scan the body would violate the one rule
that module is built around**. Do not do that.

Three candidate detections, and the choice:

1. **Header inference from `program_chain`.** Dorado in the chain suggests a
   modern basecaller, but says nothing about whether modified-base models were
   enabled for *this* run. **Rejected as a gate**: it is the `protein.faa`
   mistake — right about the common case, wrong about a legitimate one, in
   both directions (a Dorado BAM with no mods, a modkit-able BAM basecalled by
   something else).
2. **Bounded prefix scan.** Read the first N alignment records (N ~ 1000) and
   report whether any carries an `MM` tag. Cheap (kilobytes, not gigabytes),
   honest about what it checked, and correct for the realistic case where
   modification calling is a property of the basecalling run and therefore
   uniform across the file.
3. **Full scan.** Correct and unaffordable.

**Chosen: (2), a bounded prefix scan, in a new place — not in `parsers.py`.**

Put it in `modkit_runner.py` as a pure function
`has_modification_tags(bam_path, *, limit=1000) -> ModTagProbe`, returning
what it found *and* how far it looked, so the caller can say "no modification
tags in the first 1000 reads" rather than the unqualified "no modification
tags" it has not earned. The card and the launch check both call it.

**Consequence for honesty:** a BAM whose first 1000 reads lack MM but whose
later reads carry it would be refused. That is a real false negative, and it
is the right trade — the message names its own bound, so the user can see
exactly what was checked, and the alternative is a minutes-long scan on every
card render.

## Decision K2: the card is gated, and the gate refuses rather than warns

`build_methylation_card(obj)` is anchored on a completed BAM (like
`build_coverage_card`) and returns UNAVAILABLE, with a distinct reason for
each cause:

- modkit not installed → names the tool;
- the object is not a BAM → the standard kind check;
- **no MM tags in the sampled prefix** → *"No base-modification tags found in
  the first 1000 reads of this alignment. Modified-base calling has to be
  enabled at basecalling time (Dorado with a modified-base model); it cannot
  be added afterwards."*

That third message is the deliverable of criterion 3. It must say **why** the
data cannot be produced now — a user whose card says only "unavailable" will
reasonably assume the app is broken, when the real answer is that the
information was never in the file and re-basecalling is the only fix.

**The launch endpoint repeats the check.** The card is a convenience; the
launch is the gate. A graph-wired or API-driven run must hit the same refusal.

*Rejected:* offering the card unconditionally with a warning. That is exactly
the silent-empty-output failure the issue was filed to prevent — the job would
succeed, produce a bedMethyl with no rows, and record provenance saying
methylation was analysed.

## Decision K3: an empty result from a *passed* gate is still a failure

Even past the gate, modkit can produce zero sites — an aligned region with no
modifiable bases, an ML threshold that excludes everything. **A bedMethyl with
zero rows must fail the job with an honest reason, not succeed with an empty
artifact.**

This mirrors what the MultiQC spec (SF-3) settled for a run producing no HTML.
The general rule this repo keeps rediscovering: a tool exiting zero is not the
same as a tool producing a result, and the applier is where that distinction
has to be enforced.

## Decision K4: output is a bedMethyl object plus summary facts

modkit `pileup` writes bedMethyl — a BED-like per-site table of modified and
canonical counts. Two halves, both wanted:

- **The bedMethyl file** as its own `DataObject`, `derived_from=[bam.id]`.
  It is the artifact a user loads into IGV or takes to R. `FormatKind.BED`
  already exists and `_parse_tabular` already handles it.
- **Summary facts on the BAM** (`methylation_*` namespace): sites called,
  mean methylation percentage, per-modification-code breakdown (`5mC`, `5hmC`,
  `6mA` — modkit reports whichever the tags contain), and the sampled-prefix
  result from K1 so the provenance records what was checked.

**Do not** store per-site data in facts. A mammalian genome has tens of
millions of CpG sites; that belongs in the file, reachable through a report
endpoint, exactly as mosdepth serves its per-window array.

## Decision K5: track rendering reuses mosdepth's windowing, in a later stage

Methylation is naturally track-shaped, and #631 anticipated reusing the
windowed pattern. Now that mosdepth has landed, that is concrete: aggregate
per-site methylation into `gc_tracks`' `WINDOW_COUNT`/`MIN_WINDOW_BASES`
windows so the methylation track shares an x-axis with the GC and coverage
tracks already rendered per contig.

**Staged separately** (stage 2) because the bedMethyl artifact is independently
useful and the track is a larger frontend change. Stage 1 without stage 2 is a
coherent product; the reverse is not.

## Staging

| Stage | Delivers | New install? |
|---|---|---|
| 0 | Tool registration: probe + `TOOL_META` + install | yes (modkit) |
| 1 | End-to-end pileup: prefix probe, runner, handler, launch, endpoint, applier, gated card | no |
| 2 | Windowed methylation track beside GC and coverage | no |

Stage 0 closes criterion 1. Stage 1 closes criteria 2 and 3.

## Components

- **`tools.py`** — `modkit()` probe, `TOOL_META["modkit"]` with
  `pipelines=(PipelineType.UTILITY,)` (bedtools/mosdepth precedent for a
  card-invoked read-only tool). License and citation **verified against
  modkit's own repository at implementation time, not recalled** — it is an
  Oxford Nanopore tool, and ONT's licensing is not uniform across their
  repositories. `usage` describes behaviour, not flags.
- **Install** — modkit is a Rust binary distributed by ONT. **Check bioconda's
  `linux-aarch64` subdir before taking a GitHub release asset**: per CLAUDE.md,
  ONT's release assets are routinely x86-64 only, and that is the difference between the tool working
  on Apple Silicon and an arm64 skip. End the install script with a real run,
  not `--version`.
- **`modkit_runner.py`** — `build_pileup_command(...)`,
  `has_modification_tags(...)` (K1), `parse_bedmethyl(...)`,
  `summarize(...)`. All pure, all unit-tested without the binary.
- **`config.py`** — `methylation_dir` property under `bioinfo_home`, mirroring
  `coverage_dir`.
- **`queue/` handler** — `@handler("methylation", mode=SUBPROCESS,
  job_class=COMPUTE, resources=JobResources(cpu=2, mem_mb=4096, io=HEAVY),
  max_attempts=2)`. Two threads because modkit parallelises pileup and the
  single-CPU coverage precedent would leave it slow on a mammalian BAM.
- **`results.py`** — `_apply_methylation`: ingest the bedMethyl, merge summary
  facts by per-key `facts.<key>` path (never a whole-dict merge — #606),
  enforce K3.
- **`suggestion_service.py`** — `build_methylation_card` per K2, category
  `ASSEMBLY_QC` (mosdepth's, no new category surface).
- **`running_now.ENDPOINT_JOB_TYPES`** — `/pipelines/methylation`.
- **`provenance_walker`** — a **narrative verb**, not `_NO_NARRATIVE_STEP`:
  unlike `coverage`, this produces an object a person opens.
- **`node_types.NODE_TYPES`** — spec + `_launch_*` adapter, and the
  `EXCLUDED_LAUNCHES` partition run as a whole class.
- **Frontend** — `metricInfo.METRIC_INFO` entry per new `<Stat metric>`.

## Testing

- **`has_modification_tags`** against three fixture BAMs: one with MM/ML, one
  without, and one where the tags appear only after the sampled prefix — the
  last asserting the *documented* false negative rather than pretending it
  cannot happen.
- **Card, failing direction first** — UNAVAILABLE when the probe is patched
  off (patch `spec_for`, not `tools.modkit`), and UNAVAILABLE with the
  basecalling explanation when the BAM has no tags. The image will ship
  modkit, so an "available" assertion proves nothing alone.
- **Empty-result handling (K3)** — a pileup producing zero rows fails the job.
- **Registry partitions** — whole `TestExhaustiveness` class, both node_types
  and provenance.
- **Real-database check** — a real ONT BAM with modification tags end to end;
  and a BAM without them, confirming the refusal message reaches the UI.

## Verify before implementing

1. **modkit's aarch64 availability** (bioconda first).
2. **License and citation**, from ONT's repository.
3. **bedMethyl column layout** on the installed version — it has changed
   across modkit releases, and the parser is written against a captured
   fixture, not the docs.
4. **Whether `pileup` requires a reference FASTA** for the modification codes
   in play; if so, the reference-resolution seam is
   `reference_assembly.resolve_alignment_target_for_bam`.
5. **Peak RSS on a real mammalian ONT BAM**, to confirm `mem_mb=4096`.

## Out of scope

- **Differential methylation** between samples (`modkit dmr`). Needs a
  multi-sample comparison model; its own issue.
- **`modkit adjust-mods` / `repair` / other subcommands.** `pileup` is the
  one that answers the question this issue asks.
- **Re-basecalling to add modification tags.** Out of this app's scope
  entirely — the refusal message says so, which is the whole point of K2.
- **5hmC-specific downstream interpretation.** modkit reports what the tags
  contain; interpreting it is the user's.
