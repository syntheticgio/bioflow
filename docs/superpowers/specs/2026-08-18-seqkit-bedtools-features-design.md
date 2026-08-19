# seqkit/bedtools user features — design

Design for [#632](https://github.com/syntheticgio/bioflow/issues/632),
"Evaluate seqkit/bedtools as general-purpose sequence/interval utilities."

The issue recommended *against* adding either tool speculatively, and this
design honors the reasoning while changing the outcome: rather than adding
infrastructure with no consumer, it defines **four concrete user-facing
features** — each one a real Actions-tab capability — and lets the tools ride
in with them. The user chose this direction explicitly (all four features)
over the issue's own "revisit opportunistically" recommendation.

What this design deliberately does **not** do, matching the issue's
evaluation-first scope: it does not replace any existing pure-Python module.
`qc_stats.py`, `gc_tracks.py`, and `tile_scanner.py` are small, tested, and
pure — moving them behind subprocess calls would trade testability for
nothing, against the repo's stated preference (the `quast_runner.py`
pattern). The interval logic in `annotation_db.py`/`annotation_window.py` is
display-layer window querying backed by SQLite, also not bedtools' job.

## What exists today

Verified against this worktree on 2026-08-18:

- **bedtools is already installed** (`backend/Dockerfile:102`, added for
  Merqury's k-mer QV work, #64) but has **no probe in
  `pipelines/tools.py`, no `TOOL_META` entry, and no direct dispatcher** —
  only Merqury's bundled scripts call it. `/help/software` silently omits a
  shipped tool.
- **seqkit is not installed.** (The 2026-08-02 post-assembly-QC spec's
  availability table listed it, but nothing in the current Dockerfile or
  `tools.py` mentions it.)
- The Actions tab has ~18 card builders in
  `services/suggestion_service.py`, all following one shape: a
  `build_*_card(obj, ...)` pure function returning `SuggestionCard | None`,
  with availability driven by `tools.py` probes.
- Derived-object creation from a pipeline job is an established pattern:
  GenBank sequence extraction ([#348], design 2026-08-13) materializes a new
  FASTA `DataObject` with provenance; annotation subset export ([#358],
  design 2026-08-13) materializes a filtered annotation via
  `annotation_db.FeatureFilters`. Stage 4 below reuses both patterns.
- `resolve_reference()` in `suggestion_service.py` already answers "which
  reference was this BAM/VCF produced against" for the align/variant cards;
  stages 1–2 need exactly that question answered.

## Staging

Five stages, one PR each, each independently mergeable and each closing out
its slice. If priorities shift after any stage, what has landed stands
alone.

| Stage | Feature | Tool | New install? |
|---|---|---|---|
| 0 | Tool registration | bedtools + seqkit | seqkit only |
| 1 | Per-feature coverage | bedtools coverage | no |
| 2 | Variants in regions | bedtools intersect | no |
| 3 | Annotation comparison | bedtools jaccard/intersect | no |
| 4 | Region/sequence extraction | seqkit subseq | no (after stage 0) |

Stages 1–3 share one runner-module skeleton and land in descending order of
user value and ascending order of novelty; stage 4 is last because it is the
only one that creates a new data object rather than a report and the only
one needing free-form user input.

## Stage 0 — tool registration

**R0-1.** `tools.py` gains probes `bedtools()` and `seqkit()` following the
existing probe pattern (`bedtools --version`, `seqkit version`), cached by
`tool_cache` like every other probe.

**R0-2.** `TOOL_META` gains entries for both tools with `homepage`,
`citation`, `license`, and `usage` filled in, satisfying
`test_every_tool_is_documented`. License and citation are **verified against
each project's own repository at implementation time, not recalled**
(CLAUDE.md rule). `usage` describes behavior per feature stage ("computes
per-feature read coverage for the coverage report", not flag strings) and is
updated in each later stage's PR as that stage's consumer lands.

**R0-3.** The Dockerfile installs seqkit for both amd64 and arm64. seqkit
publishes static Go binaries per-arch on GitHub releases; the exact asset
names and checksums are a verify-before-implementing item, not asserted
here.

**R0-4.** A user opening `/help/software` sees both tools listed with
version, license, citation, and usage — the observable outcome of this
stage.

## Shared runner architecture (stages 1–3)

Each report stage follows the `quast_runner.py` model:

- **A pure command-builder** (`build_command(...) -> list[str]`) unit-tested
  without the binary.
- **A thin subprocess execution step** with cancellation checks, matching
  existing runners.
- **A pure parser** (`parse_output(...) -> dict`) turning bedtools' tabular
  stdout/files into the report dict, unit-tested against captured fixture
  output.

Cross-cutting obligations every stage carries (each has bitten this repo
before; see CLAUDE.md):

- **RS-1.** Every new launch function is classified in `node_types.py`, and
  the PR runs the **full `TestExhaustiveness` class**, not just the one test
  a gap names (#355/#366 trap).
- **RS-2.** Any stored sidecar/report role is present in the relevant
  registry with its exhaustiveness test updated (the STAR
  `_SIDECAR_ROLES` trap).
- **RS-3.** Every new suggestion rule has tests in
  `tests/services/test_suggestion_service.py` **in both directions**,
  including "card flips to unavailable when the probe is patched off" —
  patching `spec_for`-style seams, not frozen-at-import function objects.
- **RS-4.** Before calling a stage done, its rule is checked against the
  real database (`docker compose exec api python -c ...` with real
  objects), not only fixtures — the protein.faa/duplicate-assembly lesson.
- **RS-5.** Inputs to bedtools are **sorted, with a genome file, using the
  `-sorted` streaming path** wherever the subcommand supports it. Without
  it, bedtools loads whole inputs into memory — exactly the OOM shape
  `job_timings` exists to catch, and a silent quality regression on large
  BAMs.

## Stage 1 — per-feature coverage

The question this answers, which nothing in the app answers today: **"which
of my annotated features are poorly covered by reads?"** `bam_stats` is
genome-wide; `annotation_stats` never looks at reads.

**R1-1.** A "Feature coverage" card appears on an aligned BAM object when
the project also contains an annotation whose reference resolves (via the
same lineage machinery as `resolve_reference`) to the same assembly the BAM
was aligned against.

**R1-2.** When either half is missing, the card renders unavailable with a
reason naming the missing half ("no annotation for this reference"), same as
existing cards.

**R1-3.** The runner executes `bedtools coverage` over the annotation
against the BAM, per RS-5 (sorted inputs, genome file, `-sorted`).

**R1-4.** The report records, per feature: identifier, type, coordinates,
read count, bases covered, feature length, and breadth-of-coverage
fraction — parsed from bedtools' appended columns by the pure parser.

**R1-5.** The results view shows a sortable per-feature table, defaulting
to lowest breadth-of-coverage first, with summary stats (features at 0×,
median breadth) above it. A user with a 4,000-gene annotation can find the
uncovered genes without scrolling through covered ones.

**R1-6.** The job records into `job_timings` like every handler
(automatic), and its handler follows the existing queue-handler shape in
`queue/pipeline_handlers.py`.

## Stage 2 — variants in regions

The question: **"where do my variants land relative to annotated
features?"** This complements, not replaces, the bcftools-csq runner: csq
answers "what does this variant do to the protein" and needs well-formed
gene models; this answers "is it inside any feature" for any GFF/BED.

**R2-1.** A "Variants in regions" card appears on a VCF object when the
project contains an annotation resolving to the same reference the VCF was
called against (same resolution machinery as R1-1).

**R2-2.** The runner uses `bedtools intersect` between the VCF and the
annotation, per RS-5.

**R2-3.** The report records: total variants, variants inside ≥1 feature,
breakdown of hit counts by feature type, and per-feature variant counts for
the features that have any.

**R2-4.** The results view leads with the headline fraction ("183 of 241
variants fall within annotated features") and offers the per-type and
per-feature breakdowns beneath it.

**R2-5.** The card's `why` text distinguishes it from the consequence
(csq) card when both are applicable, so a user with both available
understands they answer different questions.

## Stage 3 — annotation comparison

The question: **"how much do these two annotations of the same assembly
agree?"** — e.g. a Bakta run versus an imported RefSeq GFF.

**R3-1.** An "Annotation comparison" card appears on an annotation object
when the project contains a second **distinct** annotation of the same
assembly. Distinctness dedups identical stored copies — the same annotation
imported twice is one annotation, not two (the duplicate-assembly lesson
from the align card, applied here at rule-writing time rather than after a
bug report).

**R3-2.** The runner computes a whole-set agreement statistic via
`bedtools jaccard` (which requires sorted inputs — RS-5 applies) and the
directional differences via `bedtools intersect -v` in both directions.

**R3-3.** The report records: the jaccard statistic, feature counts per
input, count and list of features unique to each input (list capped at a
fixed size with the cap stated in the report, so a pathological comparison
does not produce an unbounded document).

**R3-4.** The results view presents the agreement number first, then the
two unique-to lists side by side.

**R3-5.** When more than two distinct annotations exist for one assembly,
the card lets the user pick the comparison partner; with exactly two, the
partner is preselected.

## Stage 4 — region/sequence extraction

The question: **"give me these regions/sequences from this assembly as a
new FASTA."** The only stage that creates a `DataObject` rather than a
report — reusing the derived-object pattern from GenBank sequence
extraction (#348).

Two input modes, shipped in order within the stage:

**Text mode (ships first):**

**R4-1.** An "Extract sequences" action on an assembly opens a dialog
accepting, one per line: bare sequence names (whole-sequence extraction)
and `name:start-end` regions.

**R4-2.** Input is validated server-side against the assembly's actual
sequence names and lengths before any job launches; validation failures
name the offending line and the reason ("no sequence named tig00000042",
"end 5,200,000 exceeds sequence length 4,641,652"). No job is created on
invalid input.

**R4-3.** The runner executes `seqkit subseq` (regions via a generated BED
file; whole sequences via name selection) and writes one output FASTA.

**R4-4.** The output is registered as a new FASTA `DataObject` in the
project with provenance linking to the source assembly and recording the
requested regions, following the #348 derived-object pattern.

**Annotation mode (second half of the stage):**

**R4-5.** The dialog gains a second mode, available only when an
annotation of the assembly exists: filter features using the existing
`annotation_db.FeatureFilters` querying (the same filtering shipped for
annotation subset export, #358) and extract the selected features'
coordinates. Internally this materializes a BED and reuses R4-3's runner
path unchanged.

**R4-6.** In annotation mode, provenance additionally records the source
annotation and the filter that selected the features.

**R4-7.** Both modes' outputs are ordinary project objects: they appear in
the object list, can be downloaded, and are eligible inputs to any card
whose rules they satisfy (no special-casing).

## Error handling

- Tool-missing at launch time is prevented by card availability (RS-3
  direction tests); tool failure at run time surfaces through the normal
  job-failure path with stderr captured, like existing runners.
- Malformed annotation input (a GFF bedtools rejects) fails the job with
  the tool's own error preserved — no silent empty report.
- Stage 4 validation errors are user-input errors, returned synchronously
  by the API before job creation (R4-2), not job failures.

## Testing

- Pure command-builders and parsers: unit tests with fixture outputs
  captured from real bedtools/seqkit runs.
- Suggestion rules: both-direction tests per RS-3.
- Registry exhaustiveness: per RS-1/RS-2.
- Real-database spot check per RS-4 before each stage's PR merges.
- Backend suite runs via `backend/run-worktree-tests.sh` from the worktree
  (private Mongo, per CLAUDE.md); UI verification is manual against the
  worktree stack on 5273.

## Verify before implementing (not asserted above)

1. **seqkit release assets**: exact per-arch binary asset names, current
   version, and checksums, read from `shenwei356/seqkit` releases at
   implementation time.
2. **Licenses/citations for both tools**, read from each repository, per
   R0-2.
3. **bedtools version already in the image** (`bedtools --version` inside
   the api container) and that `coverage`/`intersect`/`jaccard` in that
   version accept the exact flag set the command-builders emit.
4. **`-sorted` + genome-file behavior with GFF input** on the installed
   version — confirm the streaming path works with the app's real GFF/BAM
   pairs, not just BED fixtures.
5. **VCF handling in `bedtools intersect`** on the installed version
   (plain and bgzipped), against a real VCF the app produced.

## Out of scope

- Replacing any existing pure-Python module with either tool (issue's own
  analysis; reaffirmed here).
- seqkit stats/dedup/format-conversion features — no consumer yet; same
  "necessary" criterion the issue applied.
- Annotation *editing* (declined in #358's design; unchanged).
- Multi-sample or cross-project comparisons.
