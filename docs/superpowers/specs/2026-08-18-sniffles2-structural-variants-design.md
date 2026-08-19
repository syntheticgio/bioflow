# Sniffles2 for long-read structural variant calling

Date: 2026-08-18.

Closes [#619](https://github.com/syntheticgio/bioflow/issues/619). The
short-read counterpart, [#620](https://github.com/syntheticgio/bioflow/issues/620)
(Delly or Manta), follows this one and inherits the decisions recorded here --
that issue defers its shared design questions to this document by name.

## Problem

Variant calling in BioFlow means small variants only. Clair3 and DeepVariant
call SNVs and indels, bcftools covers the short-read case, iVar handles
amplicon consensus. Nothing detects structural variants -- deletions,
insertions, duplications, inversions, translocations -- which is the variant
class long reads resolve best, and one of the main reasons a lab adopts
nanopore or PacBio in the first place.

Sniffles2 is the standard long-read SV caller and takes the alignments this
stack already produces (minimap2, winnowmap) without an intermediate step.

## Decision 1: a separate pipeline, not a fourth caller

**Structural variants get their own pipeline, node type, endpoint, card, and
results view. `variant_runner.py`, `variant_db.py`, and `vcf_stats_runner.py`
are not modified.**

#619 lists this as needing a design decision. It is the decision the rest of
this document rests on, and the reason is that the existing machinery is
SNV-shaped in a way that fails *silently* on SV input rather than loudly.

A Sniffles2 record has a symbolic ALT allele:

```
chr1  1000  Sniffles2.DEL.1  N  <DEL>  60  PASS  SVTYPE=DEL;SVLEN=-4823;END=5823;SUPPORT=17
```

Run that through what exists today:

| Code | Behaviour on an SV record |
|---|---|
| `variant_db._where`, `variant_type="snp"` | `length(ref)=1 AND length(alt)=1`. `<DEL>` is 5 characters, so no SV is ever a SNP -- correct by accident. |
| `variant_db._where`, `variant_type="indel"` | `length(ref) <> length(alt)`. **Every SV matches.** The type filter claims a 4.8 kb deletion is an indel. |
| `vcf_stats_runner.ContigDensity.add` | Same length comparison; SVs inflate the indel density track. |
| `vcf_stats_runner.parse_stats` | Reads bcftools' `number of indels:`; Ti/Tv is computed and is meaningless for SVs. |
| Everything | Nothing reads `SVTYPE`, `SVLEN`, or `END`. A 4.8 kb deletion displays as a 1 bp point event at its start position. |

None of that raises. No test fails. The single most important number about a
structural variant -- its length -- is simply absent, and the record occupies
one row in a table sorted by POS as though it were a point mutation. This is
the same shape as the registry failures recorded in `CLAUDE.md`: a hand-
maintained assumption that skips rather than raises when it meets a case it
was not written for.

Extending the existing pipeline would mean an `if is_sv` branch in every
consumer of the variant table, and the first one anybody forgets reintroduces
exactly the silent mislabeling above. A separate substrate cannot be forgotten
into.

The cost accepted: a second results view and some duplicated pagination
plumbing. The two tables never share a query, so they are not sharing code
that wants to be shared.

## Decision 2: CLR is allowed here, and refused for small variants

`variant_runner.caller_for_chemistry` refuses `ReadChemistry.CLR` outright:

> PacBio CLR reads are not suitable for variant calling: their error rate is
> too high for Clair3 or bcftools to produce reliable calls.

**That verdict inverts for structural variants, and the inversion is
deliberate.** SNV calling reads per-base accuracy, which CLR does not have.
Sniffles2 resolves breakpoints from alignment structure -- split reads and
within-read gaps -- which tolerates a high per-base error rate. CLR reads are
long, and length is the property SV detection needs.

`sv_calling_allowed_for` therefore accepts CLR while `caller_for_chemistry`
refuses it. This asymmetry must carry a comment at the function itself, not
only in this document: the two functions look like they should agree, and
someone harmonising them into consistency would silently delete a legitimate
capability with no test failing.

## Decision 3: chemistry gating

| `ReadChemistry` | Card |
|---|---|
| `HIFI`, `CLR`, `ONT_SIMPLEX`, `ONT_DUPLEX` | AVAILABLE, if the probe passes |
| `SHORT` | UNAVAILABLE -- "Sniffles2 needs long reads; short-read SV calling needs a different tool" |
| `UNKNOWN` | UNAVAILABLE -- "Unknown sequencing platform for this BAM" (the variants card's existing wording) |
| probe fails | UNAVAILABLE -- "sniffles is not installed" |

`UNKNOWN` means QC has not run. The variants card treats unknown as
short-read-and-safe; the same caution applies here for the opposite reason --
running Sniffles2 on an unrecognised BAM that turns out to be Illumina
produces junk quietly, which is the outcome worth refusing.

The `SHORT` wording is the seam #620 slots into. When Delly lands it
*replaces this reason* on the same card rather than adding a second SV card,
which is what makes #620's third success criterion ("the card correctly
distinguishes short-read from long-read SV calling, offering the right one per
project's chemistry") reachable at all.

## Decision 4: delivery, and the arm64 gap

**Sniffles is pure Python, but one of its dependencies has no arm64 wheel, so
it needs a builder stage rather than a bare `pip install`.**

Verified 2026-08-18 against PyPI:

| Package | linux x86-64 wheel | linux aarch64 wheel |
|---|---|---|
| `sniffles` 2.8.0 | pure-Python (`any`) | pure-Python (`any`) |
| `pysam` 0.24.0 | yes | yes |
| `pyspoa` 0.3.2 | yes | yes |
| **`edlib` 1.3.9.post1** | yes | **none, for any Python version** |

`edlib` publishes macOS and linux-x86_64 wheels and an sdist, and no
linux-aarch64 wheel at all. The final image has no compiler --
`build-essential` appears only in intermediate builder layers
(`backend/Dockerfile:196`, `:225`) -- so pip falls back to the sdist and the
build dies with `command 'gcc' failed`. `Dockerfile:336` already records that
exact failure for another package, and the cutadapt comment at `:59` records
the same trap with the same conclusion: a toolchain in the final image is not
worth ~200 MB to carry one tool.

This matters more than a normal portability caveat because it fails *only on
arm64*. On x86-64 CI the naive `pip install sniffles` succeeds, so the gap
would ship green and break on the maintainer's own Apple Silicon machine.

**Approach: build `edlib` from its sdist in a builder stage and copy the
resulting wheel into the final image.** This is the pattern the image already
uses -- `FROM python:3.12-slim AS winnowmap-build` at `:20`, pulled in with
`COPY --from=winnowmap-build` at `:500`, and the same shape for `legacy-ssl`
at `:465`. The toolchain stays in a stage that is discarded, the final image
grows by one small C extension, and both architectures run identical code.

Rejected alternatives:

- **Bioconda, following `install-clair3.sh`.** Arm64-capable and the template
  exists, but `Dockerfile:36` states the repo's position: bioinformatics tools
  come from Debian rather than bioconda specifically to avoid carrying a conda
  installation for a handful of tools. One SV caller does not change that
  arithmetic.
- **x86-64 only, probe reports unavailable on arm64.** Cheapest, and wrong:
  the feature would not exist on the maintainer's own machine, making it
  untestable by the person who has to verify it.

## Decision 5: license, verified rather than recalled

GitHub's API reports Sniffles' license as `NOASSERTION` and its name as
"Other". **The LICENSE file is plain MIT** -- GitHub's detector is defeated by
unconventional copyright lines (`2021- Moritz Smolka` / `2023- Hermann
Romanek`), and PyPI's own metadata for `sniffles` 2.8.0 says `MIT`.

`TOOL_META["sniffles"]["license"]` records **MIT**. This is written down
because the automated answer is wrong here, and `CLAUDE.md`'s standing rule is
to verify a license against the project's own repository rather than recall
it -- a wrong license claim on a page that reads as authoritative is worse
than a blank field.

## Components

### `tools.py`

- `sniffles()` probe: `_probe("sniffles", settings.sniffles_path, ["--version"])`,
  added to the all-tools list and to the `cache_clear` block at the bottom of
  the module.
- `TOOL_META["sniffles"]` carrying `homepage`, `repository`, `citation`,
  `citation_url`, `license` (MIT, per Decision 5), and `usage`. This is what
  `test_every_tool_is_documented` requires, and it satisfies success
  criterion 1.
- `usage` states behaviour, not flags, per `CLAUDE.md`: that BioFlow runs
  Sniffles2 against a long-read BAM and its reference to produce a typed SV
  VCF. Flags change when a runner is tuned and nothing mechanically catches a
  stale `usage` string.

### `backend/app/pipelines/sniffles_runner.py`

New module, following the `csq_runner`/`csq_parse` split #619 asks for: pure
functions over strings and paths, nothing touching the queue or the
filesystem, so command construction and progress parsing are testable without
either.

- `SnifflesParams` -- `threads`, `min_support`, `min_sv_length` (default 50,
  the conventional SV floor), optional `tandem_repeats` BED. `as_dict` /
  `from_dict` with validation, matching `Clair3Params`' existing shape.

  `min_support` defaults to Sniffles' own automatic mode (derived from
  coverage) rather than to a fixed integer. A hardcoded default would be
  wrong in both directions -- too high on a 10x callset, too low on a 100x
  one -- so the parameter exists to *override* the automatic value, and its
  unset state must reach Sniffles as "decide for me" rather than as a number
  this repo chose.

- **Output**: a bgzipped VCF plus its tabix index, stored as an object with a
  `TBI` sidecar. `SidecarRole.TBI` already exists ("the tabix index beside a
  bgzipped VCF -- to a VCF what BAI is to a BAM"), so no `SidecarRole` member
  is added. This is worth stating because `SidecarRole` is one of the
  hand-maintained registries `CLAUDE.md` warns about, and the answer here is
  that it needs no change -- not that it was overlooked.
- `build_sniffles_command(...)` -> argv, against BAM + reference + output VCF.
- `sv_calling_allowed_for(chemistry)` -- the gate from Decision 3, carrying
  the Decision 2 comment.
- A stderr progress observer in the existing `feed` / `pct` / `snapshot`
  shape.

### `backend/app/pipelines/sv_db.py`

New module, structurally modelled on `variant_db.py` -- streaming build,
indexes created after the bulk insert, journaling and synchronous writes off
because the file is a derived artifact -- but sharing none of its code, per
Decision 1.

| Column | Source |
|---|---|
| `chrom`, `pos` | CHROM, POS |
| `end` | INFO/END |
| `svtype` | INFO/SVTYPE (DEL, INS, DUP, INV, BND) |
| `svlen` | INFO/SVLEN |
| `qual`, `filter` | QUAL, FILTER |
| `support` | INFO/SUPPORT |
| `gt` | per-sample genotypes, tab-joined as the variants table does |
| `mate` | INFO/MATEID, the partner breakend of a BND translocation |

Filters: contig, position range, svtype, length range, filter value, minimum
quality. Indexes on `(chrom, pos)`, `svtype`, and `filter`.

Note that `variant_db.py`'s own justification -- millions of rows, a 32M-row
memory ceiling, streaming as the only option -- largely does not apply here,
since an SV callset is typically thousands of records. The structure is
copied for consistency and because it costs nothing, not because the scale
demands it.

Two summaries computed at build time:

- **Per-type counts** -- how many DEL / INS / DUP / INV / BND.
- **Length histogram** -- SV counts binned by absolute length on **log-scaled
  bins** (50 bp, 100 bp, 1 kb, 10 kb, 100 kb, >=1 Mb). SV sizes span five
  orders of magnitude; linear bins would put nearly every call in the first
  bar.

### Frontend

`SvResults.tsx`, `SvTable.tsx`, `SvLengthChart.tsx`.

`SvLengthChart` is modelled on `DepthHistogramChart.tsx`, which renders a
bucket array as hand-written SVG bars. The repo has **no charting library** --
`frontend/package.json` carries `cytoscape` and nothing else that plots -- and
every existing chart is built this way.

The length histogram is the SV-native counterpart of the variants view's
contig density chart, and it is what makes an SV callset readable at a glance:
a nanopore callset is dominated by sub-kb events, and a spike in the >=1 Mb
bin is usually a mapping artifact rather than biology.

`SvResults` also carries the download affordance from Decision 6: the VCF and
its TBI, offered together rather than as two unrelated files, since a viewer
needs both.

### `suggestion_service.py`

`build_structural_variants_card(obj, chemistry)`, registered in the card list
and in the `kind` -> noun map, keyed on `bam_id` like the variants card. (That
is the one launch endpoint family that does not key on `object_id`; sending
the wrong one 422s.)

### `node_types.py`

A `NodeTypeSpec` for the new launcher.

`NODE_TYPES` / `EXCLUDED_LAUNCHES` is a **partition**, not merely a covering:
every launcher must be classified, and none may be classified as both. #355
landed a spec entry and an exclusion for the same launcher in two independent
commits, satisfying the test the issue named while failing the
double-classification test in the same class; it stayed red until someone ran
the whole file (#366). The verification step below runs the full
`TestExhaustiveness` class for this reason.

## Testing

Backend tests run from a worktree with `./backend/run-worktree-tests.sh`, never
the main-checkout exec form -- the latter tests main's code from a worktree
with no error to say so.

- **Command construction** -- `build_sniffles_command` argv for default and
  non-default params.
- **Chemistry gate** -- one test per row of Decision 3's table, including
  CLR-is-allowed as its own named test, since that is the row someone will
  later "fix".
- **Card availability, in the failing direction** -- the image ships Sniffles
  installed, so a test asserting the card is *available* passes whether or not
  its patch worked. The load-bearing assertion is that the card flips to
  UNAVAILABLE when the probe is patched off. This is `CLAUDE.md`'s documented
  trap and #619's third success criterion ("with the unavailable-probe
  direction tested").
- **SV record parsing** -- symbolic ALT, negative `SVLEN` on deletions, `END`
  beyond `POS`, a BND record with a `MATEID`. These are the exact shapes that
  the existing SNV machinery mishandles, so they are the ones worth asserting.
- **Length binning** -- a call in each log bin, plus boundary values.
- **`TestExhaustiveness`, whole class** -- per `node_types.py` above.
- **`test_every_tool_is_documented`** -- satisfied by the `TOOL_META` entry.

Beyond the suite, `CLAUDE.md` asks for a check against the real database
rather than only fixtures, because hand-built objects tend to already look the
way the code expects. The equivalent here is running the finished pipeline on
a real long-read BAM and confirming the callset's lengths and types are
right -- which is success criterion 2, and the check that would have caught
every silent failure in Decision 1's table.

## Success criteria

Restating #619's, with where each is satisfied:

1. **Sniffles2 installs and passes `test_every_tool_is_documented`** --
   Decision 4 (builder stage) and the `tools.py` component.
2. **SV calling runs end-to-end on a long-read BAM** -- the runner, verified
   against a real BAM per Testing.
3. **The card offers SV calling only for long-read data, unavailable-probe
   direction tested** -- Decision 3 and the card tests.
4. **VCF stats and display represent SV records correctly, not silently
   truncated or misparsed** -- Decision 1 is the structural answer;
   `sv_db.py`'s SV-native columns and the length histogram are the concrete
   one.

One criterion beyond #619's list, from Decision 6:

5. **The callset leaves BioFlow in a form a genome browser can load** -- the
   VCF and its TBI download together, so breakpoints can be inspected in IGV.

## Decision 6: the callset is exportable for external viewers

**In scope.** The run stores a bgzipped VCF with its TBI sidecar (see the
runner's Output note), and the results view offers both as a download.

That pair is exactly what IGV, JBrowse, and every other genome browser expect
for a random-access VCF track: the TBI is what lets a viewer seek to a locus
without reading the whole file. So this is a download affordance over
artifacts the pipeline already produces, not a viewer -- BioFlow renders the
table and the length histogram, and hands off to a real browser for
breakpoint inspection, which is where that work belongs.

The obligation this creates is narrow but real: **the VCF and its index must
be downloadable together**, since a `.vcf.gz` without its `.tbi` is not a
track a viewer can load.

## Out of scope

Two capabilities were considered for this issue and deliberately deferred,
each to its own issue. Both were costed against this codebase first; the
costing is recorded because "cheap, fold it in" was the initial read on both
and was wrong in both cases.

- **Short-read SV calling** -- #620, which inherits Decisions 1, 3, and 4.

- **SV annotation.** `bcftools csq`, the existing annotation path (~1,800
  lines across five modules), computes coding consequences for *small*
  variants from a GFF and has no concept of a deletion spanning twelve genes.
  Real SV annotation means a new third-party tool, and the obvious candidate
  is heavier than a tool integration usually is here. AnnotSV v3.5.10,
  verified 2026-08-18: written in **Tcl** (a runtime this image does not
  have), **GPL-3.0** (this project's first copyleft dependency, a licensing
  call for the maintainer rather than an implementation detail), installed by
  `make install` (a fourth install shape alongside apt, pip, and vendored
  binaries), and -- decisively -- requiring a multi-gigabyte
  `Annotations_Human_*.tar.gz` fetched at build time from a single academic
  web host (`lbgi.fr`) with no checksum and no mirror.

  That last point is the DeepVariant problem: `CLAUDE.md` records a ~3 GB
  image warranting an entire on-demand sibling-container design with explicit
  user consent, specifically so a download that size is never forced on
  someone who did not ask for it. Baking a comparable bundle into the base
  image is what that design exists to prevent, so AnnotSV needs the same
  `NEEDS_INSTALL` flow -- making it roughly the size of the Sniffles work
  itself rather than a section of it.

  A lighter alternative exists and is worth evaluating in that issue rather
  than assumed here: gene-overlap annotation by intersecting breakpoints
  against a GFF the project already holds for the reference. No new tool, no
  download, no license question, and it answers "which genes does this
  deletion hit?" -- most of the practical value, without pathogenicity
  ranking.

- **Multi-sample merging** (`sniffles --combine`). One flag on the runner, and
  everything around it is new. Every request model in `api/v1/pipelines.py`
  keys on a single `object_id` or `bam_id`; the one multi-input exception
  (`hifi_bam_ids` / `nano_bam_ids`) is one assembly consuming several read
  sets, not N samples producing a joint callset. It also breaks this spec's
  storage design: `sv_db.py`'s `gt` column holds per-sample genotypes for one
  callset, and the results view has no sample picker. The failure mode of a
  half-built version is that the table shows sample 1's genotype whichever
  sample is selected -- which is precisely the bug `variant_db.py`'s `gt`
  comment records having been written to avoid.
