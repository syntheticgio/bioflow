# Circos annotation rings: repeat density and gene density

Design for [#177](https://github.com/syntheticgio/bioflow/issues/177)
(repeat-density ring) and [#179](https://github.com/syntheticgio/bioflow/issues/179)
(gene-density ring), both split out of
[#151](https://github.com/syntheticgio/bioflow/issues/151) by the
[#146](https://github.com/syntheticgio/bioflow/issues/146) design pass.

**This spec deliberately stops short of an implementation plan, and that is
the finding rather than an omission.** Both issues are blocked on a tool that
does not exist in this tree, and the blocking decision is a genuine
evaluation with real tradeoffs — not something to be settled by writing a
plan against a guessed tool. A plan naming RepeatMasker or Prokka today would
be fiction with checkboxes: every file path, every parse target, and every
resource sizing in it depends on which tool wins.

What this spec does instead is retire everything that *can* be retired without
running the evaluation: what is already decided by #151, what the real
constraints are, what the candidates are and how to choose between them, and
what the evaluation must produce before an implementation plan is worth
writing.

## What #151 already settles for both rings

Both issues are specified against "whatever windowing scheme #151
establishes." That scheme is now fixed by
[`2026-08-10-circos-gc-tracks-design.md`](2026-08-10-circos-gc-tracks-design.md),
and neither ring may introduce a second one:

- **500 windows per contig**, floored at a 100 bp minimum window width.
- **Longest 50 contigs only**, with a `*_partial` flag when contigs were
  dropped.
- **Parallel arrays of rounded numbers**, not lists of window objects — the
  document-size reasoning that applies to GC applies identically here.
- **`null` for a window with no data**, never `0`. For these rings the
  distinction is sharper than it is for GC: zero repeats and *unassessed* are
  completely different claims.
- The plot component **accepts rings as a list of descriptors**, so each of
  these adds a track rather than reworking the component.

Each ring therefore stores a `repeat_density` / `gene_density` fact shaped
exactly like `gc_tracks`, and the frontend work for each is genuinely small.
**The frontend is not the risk in either issue. The tool is.**

## The constraint that shapes both evaluations

**This is a single-user, local, Docker-based application, and the image ships
the tools.** That makes two properties dominate tool choice in a way they
would not for a hosted service or a cluster pipeline:

- **Image size and install complexity.** RepeatMasker's Dfam library is tens
  of gigabytes at full scope. Bakta's database is several gigabytes. A tool
  that triples the image is a tool that makes every user of every unrelated
  feature pay for a ring on one visualization.
- **arm64.** This repo already has precedent here: `is_arm64()`
  (`tools.py:641`) exists because Polypolish ships no arm64 build, and the
  codebase turns that into an explicit architecture note rather than a
  generic PATH error. Several of the candidates below are x86-64-only or
  poorly supported on arm64, and a Mac user on Apple silicon is a plausible
  user of this application.

There is a third option both issues should weigh honestly and neither
currently states: **on-demand delivery**. `tools.py` already has an
optional-tool-delivery mechanism (`docs/superpowers/specs/2026-08-05-optional-tool-delivery-design.md`),
so a heavyweight tool need not ship in the base image. That materially changes
the calculus for a large database — it becomes a download the user opts into
when they first want the ring, the way lineage datasets already work for
completeness scoring (`launch_completeness` refuses with "download it first"
rather than fetching inline).

**Evaluate against on-demand delivery, not against "must fit in the image."**
The latter framing rejects tools that are actually viable.

## #177 — repeat density

### Why the ring is worth having

A contig break that lines up with a repeat band tells the user the break is
resolvable with long reads. A break with no repeat under it is a data-quality
or coverage problem. That is a genuinely different next action, which is what
makes this ring diagnostic rather than decorative — and it is the argument for
paying any cost at all here.

### Candidates

| Tool | Library needed | Notes |
|---|---|---|
| **RepeatMasker** | Dfam or RepBase | The standard. Large library, slow, licensing friction on RepBase. Dfam is open. |
| **RED** (REpeat Detector) | none | *De novo*, library-free, fast. Finds repeats without classifying them. |
| **TRF** | none | Tandem repeats only — a subset, not general repeat content. |
| **k-mer frequency** | none | No tool at all: window-level frequency of high-count k-mers. |

### The recommendation to test first

**RED or a k-mer approach, not RepeatMasker.** The reasoning is that this ring
needs *density*, not *classification*. RepeatMasker's expensive part is
identifying which family each repeat belongs to, and the ring plots a single
number per window that throws that away. Paying a multi-gigabyte library and a
long runtime for information the visualization discards is the wrong trade.

The k-mer option deserves particular attention because **meryl is already
installed** — Merqury uses it, and `SidecarRole.MERYL_DB` already caches
k-mer databases per read set (`object.py:169`). A repeat proxy built from
k-mer frequency over the assembly might need no new tool at all. That should
be the first thing tested, because if it works it collapses this issue from
"new heavyweight dependency" to "new runner over an installed tool."

**What would falsify it:** if a k-mer density track does not visually
correspond to known repeat regions in a genome with a published repeat
annotation, the proxy is not measuring what the ring claims. Test against a
genome where the answer is known before building anything on it.

### What the evaluation must produce

1. A density track from the chosen approach over a real bacterial genome and a
   real eukaryotic one, compared against a published repeat annotation.
2. Measured runtime and peak memory on both.
3. Measured image-size or database-download cost.
4. arm64 availability.
5. A decision, recorded here, with the numbers behind it.

Only then is an implementation plan worth writing — and at that point it is a
short one, because the storage shape and the frontend are already fixed by
#151.

## #179 — gene density

### The trap worth restating

**BUSCO is not a substitute, and it looks like one.** `completeness_runner.py`
reports which lineage genes are present, absent, duplicated, or fragmented. It
is a completeness *score*, not a coordinate set — it cannot say where anything
sits on a contig, so no ring can be derived from it. Anyone reaching for the
existing completeness facts to build this ring is building something that
cannot exist.

### Candidates, and a scope question that comes first

| Tool | Scope | Notes |
|---|---|---|
| **Prokka** | bacterial/archaeal | Mature, widely cited, moderate database. No longer actively developed. |
| **Bakta** | bacterial/archaeal | Prokka's active successor. Several-GB database. |
| **AUGUSTUS / BRAKER** | eukaryotic | Substantially harder; needs training or a related model. |

**The scope question must be answered before the tool question: are eukaryotic
assemblies in scope?**

The recommendation is **no — bacterial and archaeal only, stated explicitly in
the UI.** Eukaryotic gene prediction is not a heavier version of the bacterial
problem, it is a different problem: it needs species-specific training,
produces results whose quality varies enormously with that training, and can
run for many hours. A gene-density ring quietly computed from an untrained
eukaryotic prediction would be a confidently drawn picture of very little.

Declaring the limit is better than a ring that is silently unreliable on half
its inputs. This also matches how #151 already gates itself on contig count —
render for the genomes where the visualization means something, render nothing
elsewhere.

Between Prokka and Bakta, **Bakta** is the better default (actively
maintained, and its database is a clean on-demand-delivery candidate), but
this should be confirmed against arm64 support and measured database size
rather than taken on reputation.

### Annotation is bigger than this ring

**Gene annotation is independently useful and probably deserves its own epic.**
It unlocks a gene-density ring, but also feature tracks, functional summaries,
and comparative work — and it is a substantial capability to introduce as a
subtask of one ring on one visualization.

The recommendation: **do not implement #179 as scoped.** Open an annotation
epic, implement annotation properly there, and let #179 become what it should
be — a small frontend task that adds a ring over data the annotation epic
already produces, in the same class as the #151 rings.

If that reframing is accepted, #179's own scope list (evaluate tool, wire
`TOOL_META`, add a suggestion rule, write a runner, parse GFF) moves to the
annotation epic almost verbatim, and only step 5 stays.

## Shared notes for whenever either lands

Both will need, per CLAUDE.md's standing traps:

- **Complete `TOOL_META`** — `homepage`, `citation`, `license`, `usage` are all
  required by `test_every_tool_is_documented`, and a new tool fails that test
  until they are filled in. **Verify the license and citation against the
  project's own repository rather than recalling them**; a wrong license claim
  on a page that reads as authoritative is worse than a blank field.
- **A `suggestion_service.py` rule**, or the tool can never be suggested and
  will never run however cleanly it installs. Test the direction that fails:
  assert the card flips to *unavailable* when the probe is patched off.
- **`backend/app/pipelines/sources.py`** has its own completeness test if a
  database source is added.
- **`docker compose restart worker`** after touching a queue handler — `worker`
  does not hot-reload.

## Status

Both issues stay **open and blocked**, labelled
`status:specification document`. Neither has an implementation plan, by
decision. The next action on each is an evaluation, not an implementation:

- **#177** — test whether a meryl k-mer density proxy tracks known repeats.
  Cheapest possible first step, and it may remove the dependency entirely.
- **#179** — decide whether to reframe as a frontend task under a new
  annotation epic. That decision is the user's, and it changes what the issue
  even is.
