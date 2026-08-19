# Salmon alignment-free RNA-seq quantification

Date: 2026-08-18.

Closes [#621](https://github.com/syntheticgio/bioflow/issues/621).

## Problem

The RNA-seq path is align -> featureCounts -> pyDESeq2 (`counts_runner.py`,
`de_runner.py`), which requires a full genomic alignment before any
quantification happens. Salmon quantifies transcript abundance directly from
reads via selective alignment, without producing a BAM, and is substantially
faster.

This does not replace the align-based path. Genomic alignment is still what
variant calling, coverage, and IGV-style viewing need. It adds a second, faster
route for users who want expression numbers and nothing else.

## Two corrections to the issue

**1. The transcriptome reference is already modelled.** #621 says the
transcriptome FASTA "may not currently be a modeled input type -- check
`sources.py`/reference-object handling". It is modelled.
`ObjectRole.TRANSCRIPT` exists (`app/models/object.py:146`) for "CDS /
transcript nucleotide sequences", and NCBI downloads already offer a CDS FASTA
component carrying that role (`app/metadata/ncbi_assembly_components.py:85`,
`file_type="CDS_NUCLEOTIDE_FASTA"`). No new reference plumbing is needed.

The real caveat is different and is scientific rather than structural: what is
downloadable is `cds_from_genomic.fna`, **not** `rna_from_genomic.fna`. CDS
sequences omit UTRs and all non-coding transcripts. See "Known limitation"
below.

**2. Salmon is packaged for Debian trixie, so there is no vendored build.**
Unlike SPAdes (`2026-08-18-spades-short-read-assembly-design.md`), which needed
a from-source arm64 build, Salmon has an apt candidate on the image's base
distro, arm64 included. Verified 2026-08-18 by installing it in a
`debian:trixie-slim` container:

```
salmon:
  Candidate: 1.10.2+ds1-1+b5
     500 http://deb.debian.org/debian trixie/main arm64 Packages
```

Installation is one line added to the existing `apt-get install` block in
`backend/Dockerfile`, alongside `subread`, `hisat2`, and `rna-star`. The CLI
was verified against the installed binary rather than recalled:

```
$ salmon --version
salmon 1.10.2
$ salmon index --help
  -t [ --transcripts ] arg      Transcript fasta file.
  -i [ --index ] arg            salmon index.
```

`--version` exits zero and prints a bare `salmon 1.10.2`, so `_probe`'s
standard path applies with no special-casing (unlike featureCounts, which exits
non-zero on `-v`).

## Decisions

### Salmon, not kallisto

Per the issue's own recommendation. Salmon is what current nf-core-style
pipelines use, has selective alignment and GC-bias correction, and is packaged
for trixie. Only one implementation is being added, so **no quantifier registry
abstraction** is built -- one implementation is not a registry, and
`aligner_registry.py` exists because there are five aligners.

### Summarize to gene level, feeding the existing DE path

Salmon emits fractional, transcript-level `NumReads`. pyDESeq2 here consumes
`SampleCounts.counts: dict[str, int]` -- integer, per gene
(`de_runner.py:51`). The bridge is a tximport-equivalent summarization inside
the Salmon runner: map transcript -> gene, sum `NumReads` per gene, round to
integer, emit an ordinary `ObjectRole.COUNTS` object.

Rejected alternatives:

- *Widen `de_runner` to accept floats and transcript-level features.* Changes a
  shared, heavily-guarded module and yields results keyed by transcript, which
  is not what most users want from a DE table.
- *A separate DE entry point for Salmon.* Duplicates the pyDESeq2 plumbing and
  splits the UI for no gain.

The existing `differential_expression` node needs **no change**: its input port
keys on `ObjectRole.COUNTS`, not on which tool produced the file
(`node_types.py:637`).

### `annotation_sha256` carries the transcriptome digest

`merge_counts` refuses to merge samples whose `annotation_sha256` differs, at
zero tolerance (`de_runner.py:167`, `GENE_SET_TOLERANCE = 0`). Salmon has no
annotation; it has a transcriptome. That field is filled with the transcriptome
FASTA's digest.

This is correct in both directions. Within the Salmon path, two samples
quantified against different transcriptomes are refused, which is the behaviour
the gate exists for. Across paths, a matrix mixing Salmon and featureCounts
samples is also refused, because their digests can never match -- and those two
genuinely are not the same gene universe, so refusing is right rather than
merely conservative.

The field keeps its name; live data sits behind it. Its docstring records the
widened meaning.

## The transcript-to-gene map is the load-bearing risk

Salmon reports abundance keyed by the sequence IDs in the FASTA it indexed. For
the summarized output to describe **the same gene universe featureCounts
produces**, the map must land on the identifier featureCounts groups by --
`gene_id` for GTF, `locus_tag` for NCBI GFF3, per
`counts_runner.attributes_for_format`.

NCBI CDS FASTA deflines carry bracketed attributes (`[gene=...]`,
`[locus_tag=...]`) after an `lcl|`-prefixed sequence ID. **This was not
verified against a real file** -- no CDS FASTA exists on this machine's data
volume, and the codebase contains no existing parser for that header shape
(a `grep` for `lcl|` and `[locus_tag` returns nothing in `app/`).

Two consequences, both requirements:

- **REQ-TX2GENE-1.** `parse_tx2gene` must raise `ValidationError` naming the
  first unparseable defline. It must **not** fall back to using the transcript
  ID as its own gene ID. That fallback produces a counts file with thousands of
  single-transcript "genes" which merges cleanly, passes every downstream sanity
  check, and is wrong -- the silent-success failure class this repo has hit
  repeatedly (`_SIDECAR_ROLES`/STAR, the `protein.faa` reference picker).
- **REQ-TX2GENE-2.** The implementation plan must include downloading a real
  NCBI CDS FASTA and verifying the defline format before `parse_tx2gene` is
  considered done. This is a verification step, not an assumption to code
  against.

## Components

### `backend/app/pipelines/salmon_runner.py`

Pure functions over strings, paths and dicts -- no queue, no filesystem --
matching `counts_runner.py` and `variant_runner.py`.

| Function | Contract |
|---|---|
| `index_command(transcriptome, index_dir, ...)` | `salmon index -t <fa> -i <dir>` |
| `quant_command(index_dir, reads, out_dir, ...)` | `salmon quant -i <idx> -l A <reads> -o <dir>` |
| `parse_quant(text)` | `quant.sf` -> `({transcript_id: float}, facts)` |
| `parse_tx2gene(fasta_headers)` | deflines -> `{transcript_id: gene_id}`, or raises |
| `summarize_to_gene(per_tx, tx2gene)` | -> `{gene_id: int}`, summed then rounded |
| `command_line(cmd)` | `shlex.join`, for provenance |

`-l A` (automatic library-type detection) is deliberate. The featureCounts path
needs `strandedness_for_align_params` because a wrong `-s` yields near-zero
counts that look like a failed experiment rather than a parameter error. Salmon
detects orientation itself; what it inferred is recorded as a fact so the value
is visible rather than merely applied.

`quant.sf` columns are `Name, Length, EffectiveLength, TPM, NumReads`. Facts
recorded: `transcripts_detected`, `mapping_rate`, `library_type_inferred`.

### `tools.py`

`salmon()` probe on the standard `@lru_cache(maxsize=1)` + `_probe` pattern,
`settings.salmon_path`, added to the aggregate tool list. A `TOOL_META` entry
is required by `test_every_tool_is_documented`, which gates on `homepage`,
`citation`, `license`, `usage`.

Verified against upstream 2026-08-18 via `gh api repos/COMBINE-lab/salmon`
rather than recalled, per CLAUDE.md:

- license: **BSD-3-Clause**
- homepage: `https://combine-lab.github.io/salmon`
- repository: `https://github.com/COMBINE-lab/salmon`
- citation: Patro R, Duggal G, Love MI, Irizarry RA, Kingsford C. Salmon
  provides fast and bias-aware quantification of transcript expression. Nature
  Methods. 2017;14(4):417-419. (`https://doi.org/10.1038/nmeth.4197`)

`usage` states behaviour, not flags: one sample per job, index built once per
transcriptome and reused, library type auto-detected, and -- explicitly -- that
a CDS-only reference quantifies coding transcripts only.

### `node_types.py`

A new `salmon_quantify` spec rather than overloading `quantify`. The port sets
are genuinely different (FASTQ + TRANSCRIPT FASTA versus BAM + GTF), and one
spec carrying two mutually exclusive port sets would make the graph model lie
about what connects to what.

- inputs: `reads` (`FormatKind.FASTQ`); `transcriptome` (`FormatKind.FASTA`,
  `role=ObjectRole.TRANSCRIPT`)
- outputs: `counts` (`FormatKind.TEXT`, `role=ObjectRole.COUNTS`)

The output port type is identical to `quantify`'s, which is what lets the
existing DE node consume it.

**REQ-NODE-1.** This adds a launcher, so the `NODE_TYPES`/`EXCLUDED_LAUNCHES`
partition applies. The full `TestExhaustiveness` class must be run, not only
`test_every_launch_function_is_classified` -- per CLAUDE.md, #355 satisfied that
test while silently failing `test_no_launcher_is_both_used_and_excluded` in the
same class, and it stayed red until someone ran the whole file (#366).

### Index caching

`salmon index` is per-transcriptome and expensive; `salmon quant` is per-sample
and cheap. The index is stored as a sidecar against the transcriptome object and
reused across samples.

**REQ-INDEX-1.** This needs a `SidecarRole` member. `results._SIDECAR_ROLES` is
the derivable case (`{role.value: role for role in SidecarRole}`) so it should
be covered automatically -- but this is the exact registry that cost STAR's
`build_index` job all eight of its index files while reporting success. The
exhaustiveness assertion must be confirmed to hold, not assumed.

### `suggestion_service.py`

A card rule offering Salmon when a project holds FASTQ reads and a
TRANSCRIPT-role FASTA. Per CLAUDE.md, a tool no rule can pick is never suggested
however cleanly it installs.

**REQ-CARD-1.** `protein.faa` must not be picked as a transcriptome. It is
FASTA with `ObjectRole.PROTEIN`; the role filter handles it, and a test must
prove it -- this is the same bug shape that made the Actions tab count
`protein.faa` as an alignable reference.

**REQ-CARD-2.** The same transcriptome registered twice must count as one
usable reference, not two.

**REQ-CARD-3.** Rules are verified against the real database with a one-off
`python -c` probe run inside the `api` container, not only against fixtures.
Both prior suggestion-rule bugs passed a full green suite because their
fixtures already looked the way the rules expected.

## Testing

Run from the worktree with `./backend/run-worktree-tests.sh`, never the
main-checkout `exec api` form, which silently tests main's tree.

- Unit tests for every pure function above, including `parse_tx2gene`'s
  refusal path (REQ-TX2GENE-1).
- `test_every_tool_is_documented` for the `TOOL_META` entry.
- The full `TestExhaustiveness` class in `test_node_types.py` (REQ-NODE-1).
- Suggestion rules in `test_suggestion_service.py` covering REQ-CARD-1 and -2.
- **Availability tests assert the card flips to _unavailable_ when the probe is
  patched off.** The image ships most tools installed, so asserting a card is
  available passes whether or not the patch worked; the unavailable direction is
  the one that fails when the seam breaks.
- End-to-end: index + quant on a real transcriptome producing transcript-level
  estimates, summarized and fed through pyDESeq2 (issue success criteria 2, 3).

## Known limitation

Quantifying RNA-seq against **CDS-only** sequences drops UTRs and all
non-coding transcripts, biasing abundance estimates in a way that is invisible
in the output. This is accepted for v1 because CDS FASTA is what is already
modelled and downloadable, but it is a real scientific caveat rather than a
documentation nicety.

It must be stated where a user actually reads it: `ToolMeta.usage` (rendered on
`/help/software`) and the suggestion card copy. It is also the strongest
argument for the `rna_from_genomic` follow-up below.

## Out of scope

Each is a defensible follow-up; none belongs in the first vertical slice.

- **kallisto.** The issue recommends Salmon; a second implementation is a
  separate decision.
- **A quantifier registry abstraction.** Premature at one implementation.
- **`rna_from_genomic` as a new NCBI component.** Would give a true
  transcriptome and resolve the limitation above, but touches the
  `COMPONENTS`/`COMPONENT_ORDER` registry pair CLAUDE.md specifically warns
  about, where a component added to one and not the other is invisible in the
  download dialog with no error anywhere.
- **gffread-derived transcriptomes** from genome + GTF. Adds a second new tool
  and another node type.

## Success criteria

Restated from #621, with the verification for each:

1. Salmon installs and passes `test_every_tool_is_documented`.
2. `salmon index` + `salmon quant` run end-to-end producing transcript-level
   abundance estimates.
3. Output feeds differential expression via pyDESeq2 -- through gene-level
   summarization into the existing `ObjectRole.COUNTS` path, with no change to
   `de_runner.py`.
4. A suggestion card offers Salmon for RNA-seq data where a TRANSCRIPT-role
   reference is available, and does not offer it for `protein.faa`.
