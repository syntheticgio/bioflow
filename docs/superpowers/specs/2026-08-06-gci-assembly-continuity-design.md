# GCI assembly continuity inspection — design

GitHub [#65](https://github.com/syntheticgio/bioflow/issues/65), slice 4 of
epic [#13](https://github.com/syntheticgio/bioflow/issues/13). Follows
[`2026-08-05-remaining-post-assembly-qc-design.md`](2026-08-05-remaining-post-assembly-qc-design.md),
and the slices that shipped before it: QUAST
([#62](https://github.com/syntheticgio/bioflow/issues/62)), CRAQ
([#63](https://github.com/syntheticgio/bioflow/issues/63),
[design](2026-08-06-craq-assembly-error-detection-design.md)), and Merqury
([#64](https://github.com/syntheticgio/bioflow/issues/64),
[design](2026-08-06-merqury-kmer-qv-design.md)).

Upstream facts below were read from GCI's README, its FAQ, and its repository
metadata via `gh api` on 2026-08-06, not recalled.

## The gating question, answered

The issue is explicit that this slice is blocked on one question and should
not be labelled ready without it:

> **Is a minimap2-only GCI run methodologically honest, or a misuse of the
> tool?** That is an upstream-reading question, not a build question, and it
> decides whether this slice costs one source build or none.

**Answered: it is honest, with a disclosed sensitivity cost, and the slice
costs no source build.** Two findings, and the first invalidates the issue's
stated premise.

### GCI never invokes an aligner

The issue says *"The real prerequisite is a second aligner"*, because
winnowmap has no Debian candidate. That reads GCI's Requirements list as a
dependency list. It is not. Verbatim from the README:

- `minimap2` — "(optional, but wanted for mapping)"
- `winnowmap` — "(optional, but wanted for mapping)"
- `veritymap` — "(optional, for mapping)"

**Winnowmap carries the same parenthetical as minimap2**, which BioFlow has
had installed since the alignment slice. Every aligner in that list is a
suggestion for *producing GCI's input*, not a dependency of the program. GCI
consumes finished alignments — `--hifi` and `--nano` take BAM and PAF files.
What `GCI.py` itself requires is: python3, pysam, biopython, numpy,
matplotlib.

So there is **no source build**, and the "standard third-party-build
checklist, including the arm64 asset check that has bitten this repo three
times" that the issue says this slice inherits does not apply. GCI is pure
Python, MIT licensed, and additionally packaged on bioconda.

### But two aligners is upstream's recommendation, and it says why

"Not required" is not "equivalent," and the FAQ answers this directly rather
than leaving it to inference. FAQ #2, "How to select two aligners?", reports
a real benchmark on rice MH63:

- Winnowmap2 and VerityMap are both "specially developed for complex
  regions"; minimap2 is not.
- Runtimes measured upstream: minimap2 0.17 h, Winnowmap2 3.07 h (including
  its meryl k-mer library), VerityMap 4.5 h.
- "WM2+MM2 and VM+MM2 yielded similar potential assembly issues and GCI
  scores, while WM2+VM detected fewer issues with a higher GCI score.
  Therefore, the combination between WM2 and MM2 is recommended."

And from the benchmark table's note: results from one winnowmap bam plus one
minimap2 paf "would be sightly higher than all bams."

The honest reading, which is what this design is built on:

- A **two-aligner** run is what GCI's published benchmarks and scores mean.
  Two aligners cross-check each other in repetitive regions, and that
  cross-check is the method's core.
- A **minimap2-only** run is a legitimate, upstream-supported invocation.
  Single-alignment input is explicitly allowed, and the shipped test-data
  example runs one bam plus one paf. It is **less sensitive in repetitive
  regions** — which is precisely where continuity errors concentrate.

That makes this a **disclosure problem, not a build problem** — the same
shape CRAQ's single-library policy already solved in this repo.

## What that means for the design

Ship minimap2-only, and record that it was minimap2-only.

- `assembly_continuity_aligners: list[str]` records what actually produced
  the input alignments, the same way CRAQ's `has_ngs`/`has_sms` record what
  it was given.
- The UI states that the score is single-aligner and undercounts issues in
  repetitive regions, citing the FAQ's finding rather than a guess.

### Do not omit the score the way CRAQ omits CSE

CRAQ's rule was *absent means unmeasured*: an NGS-only run does not write
`assembly_error_cse_count` at all, because upstream says CSE is "hardly
detected" without long reads, and a stored `0` would eventually be read by
something that had lost the caveat.

**Upstream says something materially different here.** WM2+MM2 and VM+MM2
"yielded similar potential assembly issues and GCI scores" — a minimap2-paired
run is a real measurement with a known and modest bias direction, not a
non-measurement. Storing it with the aligner list attached is the honest
encoding; omitting it would discard a valid number.

The distinction between these two slices is worth stating plainly, because
"follow CRAQ's precedent" applied mechanically would get it backwards: the
omission rule is driven by *what upstream says the degraded mode measures*,
not by a general preference for omitting things.

### Winnowmap as a later enhancement

Winnowmap would raise sensitivity and move a run onto upstream's recommended
footing. It is out of scope here, and worth filing separately if wanted.

**One thing to carry forward:** winnowmap's unusual dependency is meryl — its
own documented setup runs `meryl count` to build the repetitive-k-mer list it
takes via `-W`. Merqury's slice ([#64](https://github.com/syntheticgio/bioflow/issues/64))
installs Marbl meryl 1.4.2, including an arm64 binary. So **after #64 lands,
winnowmap's only remaining blocker is winnowmap itself.** That is recorded in
both designs.

## On T2T calibration

GCI describes itself as "an assembly assessment tool for high-quality genomes
(e.g. T2T genomes)", and its paper is titled "GCI: a continuity inspector for
complete genome assembly."

An earlier framing of this slice used that to argue against building it at
all, on the grounds that BioFlow's assemblies are Flye drafts. **That
reasoning was wrong and is recorded here so it is not repeated.** BioFlow is a
platform: a user can ingest any assembly, including a published T2T genome,
and more assemblers are expected. Reasoning from the current default
assembler to the tool's usefulness confuses today's common case with the
software's input space.

The calibration is still real, and it belongs in **interpretation**. GCI's
published benchmarks span 7.26 to 99.99 across real T2T assemblies — a range
wide enough that a bare number means little without context. So the UI shows
the score alongside that benchmark range, and **invents no quality bands**.
CRAQ's slice could adopt upstream's published AQI bands (>90 reference
quality, 80–90 high, 60–80 draft, <60 low) because upstream publishes them.
GCI publishes no such scale, so none is fabricated here.

## Inputs

Long-read BAMs aligned against the assembly, resolved through
`pipeline_service.reference_for_bam` (`pipeline_service.py:1685`) — the
validated-provenance lookup CRAQ's slice established, so "is this BAM
actually against the assembly under test?" is a lookup rather than trust.
Uploaded BAMs with no provenance are not eligible; `reference_for_bam` returns
None and the card treats that as not available rather than guessing.

This works today because `queue/results.py:1271` roles a de novo assembly's FASTA
`REFERENCE` on ingest and `_check_reference` requires only `READY` plus a
FASTA kind — so the ordinary align pipeline already produces sorted, indexed
BAMs against a user's own assembly.

### Chemistry routing is stricter than CRAQ's

GCI has exactly two input slots and **no short-read input exists at all**.
`pipeline_service.read_chemistry_for_alignment` (`pipeline_service.py:777`)
answers the routing question:

| `ReadChemistry` | Routing |
| --- | --- |
| `HIFI` | `--hifi` |
| `ONT_SIMPLEX`, `ONT_DUPLEX` | `--nano` |
| `CLR` | **Refuse.** PacBio CLR is not HiFi and has no slot |
| `SHORT` | Ineligible — the card does not offer it |
| `UNKNOWN` / `None` | The dialog asks |

`CLR` is the case that needs stating, because it is long-read and therefore
looks eligible. Routing CLR to `--hifi` would mislabel the evidence: GCI's
filters assume HiFi-grade per-read accuracy, and CLR's error profile is
nothing like it. Refusing is correct; guessing produces a confidently wrong
score.

The `UNKNOWN` case follows CRAQ's rule exactly. That function's docstring
says callers "fall back to the conservative short-read default rather than
guessing" — right for picking an alignment preset, wrong here, and doubly so
when `SHORT` is not even a valid input.

Both slots may be filled. Upstream supports HiFi + ONT together, and it
changes the output file set — `_hifi`, `_nano` and `_two_type` depth files,
with the two-type file giving the headline score.

### Pairing policy

Auto-pair when unambiguous, ask otherwise, following CRAQ:

- Exactly one eligible BAM per slot → the card fires, no dialog.
- Exactly one BAM of a single type → the card fires single-type.
- Anything else — two candidates for one slot, unknown chemistry — → dialog
  with a chooser. The card never guesses.

## Facts written

On the assembly object, merged, never replacing:

| Fact | Type | Notes |
| --- | --- | --- |
| `assembly_continuity_gci` | float | Headline score; from the two-type file when both slots ran |
| `assembly_continuity_gci_hifi` | float | Written only when that slot ran |
| `assembly_continuity_gci_nano` | float | Written only when that slot ran |
| `assembly_continuity_expected_n50` | int | GCI's own, **not** the ingest-time contiguity facts |
| `assembly_continuity_observed_n50` | int | |
| `assembly_continuity_expected_contigs` | int | |
| `assembly_continuity_observed_contigs` | int | |
| `assembly_continuity_issues` | int | Regions below the depth threshold |
| `assembly_continuity_aligners` | list[str] | |
| `assembly_continuity_map_qual` | int | |
| `assembly_continuity_threshold` | int | |
| `assembly_continuity_tool` | str | `"gci"` |
| `assembly_continuity_tool_version` | str | |

**The N50s are GCI's own and must not be written into the `sequence_*`
namespace.** `_parse_fasta` already computes contiguity for every FASTA at
ingest. GCI's expected/observed N50 are a different computation for a
different purpose — the observed N50 is derived from the *filtered depth*, not
from the contig lengths. Writing them into the shared namespace would
reintroduce exactly the "two facts that are supposed to agree, on one object"
bug the epic recorded when it deleted `assembly_n50`.

**`-mq` belongs with the score, not in a config file.** Upstream's README
note on [issue #21](https://github.com/yeeus/GCI/issues/21) is explicit: `-mq`
"is not just a 'keep more reads → cover more bases' switch" — lowering it
pulls in multi-mapping reads from repetitive and low-complexity regions, and
"for uniformity analysis, using a strict MQ threshold (e.g., 30-60) is often
more interpretable." Two runs at different `-mq` are not comparable, which is
the same argument QUAST's slice made for `--min-contig`. Default 30, recorded
as a fact.

**Counts and N50s parse as `int`, scores as `float`, asserted with
`isinstance`, not equality** — the gap QUAST's slice shipped and CRAQ's
closed.

## Reports

`-p -it pdf` emits per-chromosome filtered-depth plots. Upstream recommends
PDF: "PDF is recommended because PNG file may lose some details though GCI
will output png files by default."

PDFs and PNGs are static, so like Merqury's slice and unlike QUAST's this
needs **no CSP exception and no `sandbox` climbdown** — they go under
`qc_reports/<object_id>/` and are served by the existing route with none of
QUAST's scripting exposure.

**Plots are opt-in and gated on contig count.** GCI emits one image per
chromosome, so a fragmented assembly with hundreds of contigs produces
hundreds of files. Default on below a contig threshold, off above it, and
overridable from the dialog. A QC job that quietly writes 800 PDFs is a
storage surprise, not a feature.

**Input is linked under a fixed name** so a hostile filename never reaches an
output path — QUAST's lesson, applied before the bug exists, as CRAQ's and
Merqury's slices also do.

## Actions card

`build_continuity_card`, keyed on the assembly object. Unavailable reasons,
each saying what the user can do:

- GCI not installed → the probe's error.
- No long-read BAM against this assembly → "Continuity inspection needs long
  reads aligned to this assembly. Align a HiFi or ONT read set against it
  first."
- Only short-read BAMs → says so specifically, since the generic message
  would send the user to re-run an alignment that cannot help.
- BAMs exist but chemistry is unknown → available, routed to the dialog.

Registered in the card list in `suggestion_service.py`. **This is the step
that is silently skippable** — CLAUDE.md records that installing a tool
without a rule that can pick it leaves a card reading "no tool installed"
beside an installed tool.

## Slice shape

A single-tool slice, no registry — the same reasoning QUAST and CRAQ
recorded. `assembly_qc_registry` models completeness, and its `odb` field
means nothing to a tool whose parameters are two alignment slots and a
mapping-quality floor.

| Piece | Location |
| --- | --- |
| Install | `backend/scripts/install-gci.sh`, called from `backend/Dockerfile` |
| Probe + metadata | `tools.gci()`, `TOOL_META["gci"]` |
| Command + parsers | `backend/app/pipelines/gci_runner.py` |
| Handler | `assembly_qc_handlers.assess_assembly_continuity` |
| Launch | `pipeline_service.launch_continuity_qc` |
| Route | `POST /pipelines/assembly-continuity` |
| Card | `suggestion_service.build_continuity_card` |
| UI | `AssemblyFacts.tsx` block + a dialog |

`TOOL_META["gci"]` needs `homepage`, `citation`, `license`, `usage` or
`test_every_tool_is_documented` fails:

- Homepage / repository: `https://github.com/yeeus/GCI`
- License: **MIT** (verified: `gh api repos/yeeus/GCI` → `spdx_id: MIT`)
- Citation: Chen, Quanyu, et al. "GCI: a continuity inspector for complete
  genome assembly." *Bioinformatics* 40.11 (2024): btae633.
  `https://doi.org/10.1093/bioinformatics/btae633`
- `usage` describes behaviour, not flags: GCI is run against BioFlow-produced
  long-read BAMs only, minimap2-only alignments, plotting gated on contig
  count.

### Pin a commit SHA, not the tag

The repository's newest tag is `v1.0`, but `pushed_at` is 2026-02-28 —
commits have landed since, and the README documents behaviour that appears to
postdate the tag (the `-mq` guidance citing issue #21). Pin a **commit SHA**
and record which one, the way `install-craq.sh` pins its commit. Bioconda's
recipe is a supported alternative, but this image carries no conda, and adding
one for a pure-Python tool would cost far more than a pinned clone.

Its Python dependencies (pysam, biopython, numpy, matplotlib) are ordinary
pip installs. Check which the image already has before adding all four.

## Testing

- **Runner**: chemistry routing including the `CLR` refusal; fixed input
  filenames; `-mq` emitted and recorded; plots gated on contig count.
- **Parsers**: typed with `isinstance`; the single-type and two-type output
  shapes are different file sets and both need covering.
- **Launch path**: refuses unknown and `CLR` chemistry; validates each BAM's
  `derived_from` actually contains the assembly.
- **Card**: flips to unavailable when the probe is patched off; dark with no
  long-read BAM; the short-read-only case produces its specific message.
- **A real-data check against the running stack**, per CLAUDE.md.

**Use upstream's real test data for the parser fixture.** GCI ships example
data on [Zenodo](https://zenodo.org/records/12748594) with expected outputs
committed under `example/`, and the README gives an exact command that
reproduces them. A hand-built `.gci` fixture proves only that the parser
matches the fixture author's memory of the format — which is precisely how
CRAQ's slice shipped a parser looking for a summary row keyed `all` when
every real report keys it `Genome`, with every unit test green.

## Verify before implementing

- **The `.gci` file's exact column layout**, from a real run against the
  Zenodo test data rather than from the README's prose description of it.
- **Whether `GCI.py -v` prints a usable version string**, and what a pinned
  commit reports — the probe depends on it.
- **Runtime and RAM on a realistic assembly.** The FAQ has a RAM/time figure
  at 32 threads with a note that 16 suffices, and "reduce memory usage" is on
  upstream's own to-do list. This matters for `resource_estimator`.

## Non-goals

- **Winnowmap, VerityMap, and any second aligner.** Discussed above; a
  separate enhancement whose blocker shrinks once #64 lands meryl.
- **Trio binning** (canu, seqkit). GCI's README documents it for
  haplotype-resolved assemblies; BioFlow has no parental read-set concept,
  the same reason Merqury's trio mode is out of scope.
- **GCI's utility scripts** — `filter_bam.py`, `plot_depth.py`,
  `GCI_score.py`, `convert_samtools_depth.py`. `bamsnap` in particular is an
  optional visualization dependency that stays out of the image.
- **Regions mode (`-R`) and chromosome selection (`--chrs`).** Whole-assembly
  only for this slice.
- **Contamination screening.** Named non-goal of #13.

## Closeout, 2026-08-07

Shipped. See `docs/TODO-done.md` for the full closeout entry (moved there
from `docs/TODO.md` per this repo's convention for a fully-resolved entry).

What the implementation found that this spec and its plan did not know yet,
both from actually running the thing rather than re-reading either document:

- **The `.gci` column layout, unverified above, held.** A real run against
  GCI's own Zenodo example (MH63 rice assembly) produced output the parser
  written from this spec's prose handled correctly without a code change —
  the parser's existing `fields[0] != "Genome"` filter happened to already
  skip an undocumented leading `"HiFi:"` label line and a trailing
  dash-separator line, neither anticipated here. Locked in with a regression
  test built from the real captured output.
- **`GCI.py -v` unverified above: confirmed to print `GCI version 1.0`,
  exit 0.** The probe's `["--version"]` args needed no adjustment.
- **Runtime/RAM unverified above: not resolved by this slice.** The Zenodo
  example is subsampled and completed in under a minute against the
  declared `JobResources(cpu=8, mem_mb=16384)`, nowhere near stressing
  either figure — a real production-scale genome run still needs separate
  measurement, left open rather than guessed at from an unrepresentative
  run.
