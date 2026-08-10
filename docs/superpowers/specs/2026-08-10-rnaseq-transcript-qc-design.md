# RNA-seq transcript QC: gene body coverage + genomic feature distribution — design

**Date:** 2026-08-10
**Status:** Approved, not implemented
**Issues:** [#158](https://github.com/syntheticgio/bioflow/issues/158),
[#159](https://github.com/syntheticgio/bioflow/issues/159) (epic
[#154](https://github.com/syntheticgio/bioflow/issues/154))

Two RNA-seq alignment QC charts, specced and built together:

- **Gene body coverage** — mean coverage normalized across each transcript's
  length, 5' to 3'. A sharp 3' peak indicates RNA degradation before
  sequencing, since poly-A selection captures only the surviving 3' tail.
- **Genomic feature distribution** — the share of mapped reads falling in
  exonic, intronic, and intergenic regions. mRNA should be overwhelmingly
  exonic; high intronic or intergenic fractions suggest genomic DNA
  contamination or immature pre-mRNA.

## Why one spec and one job

#159 already proposes this, and the code agrees. Both metrics need the same
two expensive things: a parsed transcript model from a GTF, and a pass over
the BAM classifying each read against it. Computing them separately means
parsing the GTF twice and traversing the BAM twice for two charts that appear
side by side and are gated by identical conditions.

One job, one GTF parse, one BAM pass, two facts.

## Tooling: custom pysam, not RSeQC

RSeQC's `geneBody_coverage.py` and `read_distribution.py` are the reference
implementations, but adding RSeQC means a new system dependency, a `TOOL_META`
entry with license/citation/usage, `suggestion_service` wiring, and the
Software help page's completeness test — for two curves that are a few dozen
lines over pysam.

Custom pysam also matches how this repo actually built the comparable
features: insert size and MAPQ distributions are computed in
`backend/app/storage/sequence_stats.py` with pysam, not by adding
`samtools stats` as the sibling tickets assumed. Following that precedent
keeps the tool surface flat.

Consequence to accept honestly: the numbers will not match RSeQC's to the
decimal, because bin counts and read-assignment tie-breaking are
implementation choices. These charts are read for *shape* (is there a 3'
cliff; is the exonic fraction dominant), not for a number quoted in a methods
section. The spec fixes the choices below so the shape is stable and the
implementation is testable.

## The gating problem, and what the database actually says

Both tickets note there is no stored RNA-vs-DNA flag, citing
`pipeline_service.py`'s comment that this "is not knowable from the bytes."
That comment is about format detection and is still true, but the metadata
layer has moved since: `molecule_type` (DNA / RNA / Other) **is implemented**
— `metadata/sra.py` maps it from the SRA record's `LIBRARY_SOURCE`, and
`api/v1/objects.py` exposes an inference endpoint.

Checking the real database rather than the schema is what makes this
specifiable, and it contradicts the obvious design:

| | populated on BAMs |
|---|---|
| `molecule_type` | **0 of 9** |
| `assay` | **9 of 9** — `RNA-seq`, `WGS`, `ChIP-seq` |

`molecule_type` only lands when an SRA record supplies it; these BAMs were
ingested without one. Gating on `molecule_type` alone would ship a feature
that never appears for anyone currently using the app — passing tests, no
visible button. `assay` meanwhile is fully populated and perfectly
discriminating on this data (`RNA-seq` on exactly the STAR-aligned objects).

Also relevant: **there are zero GTF objects in the database.** GTF
availability is a live gate, not a formality, and the empty state is the state
every current user is in. It must read as a clear next action, not as a
missing feature.

### Applicability chain

Evaluated in order, first hit wins:

1. `metadata.molecule_type == "RNA"` → applicable. Authoritative when present.
2. `metadata.molecule_type` in `{"DNA", "Other"}` → not applicable. An
   explicit DNA answer outranks any inference below.
3. `metadata.assay == "RNA-seq"` → applicable.
4. `facts.aligned_by` in `{star, hisat2}` → applicable. Weakest signal, and
   the only one the tickets proposed; a splice-aware aligner is evidence, not
   proof.
5. Otherwise → not applicable.

For #159 specifically, ChIP-seq is a legitimate consumer of the feature
distribution chart (it is DNA, so gene body coverage is meaningless for it).
`assay == "ChIP-seq"` therefore enables the feature-distribution chart only.
This is the one place the two charts' gating differs, and it is why the job
writes two independent facts rather than one combined blob.

### Not automatic

The job is **on demand**, behind a button, exactly like the existing "Compute
results" flow in `BamResults.tsx` — not enqueued automatically after
alignment.

Rationale: the chain above is inference, and steps 3-4 can be wrong. An
automatic job that fires on a mis-labelled DNA BAM burns a full BAM pass and
renders a meaningless curve the user must learn to distrust. A button turns a
soft signal into a suggestion the user confirms. It also sidesteps needing a
correct answer for every historical BAM before the feature can ship.

## GTF selection

The transcript model comes from a GTF object in the same project
(`format.kind == "gtf"`). Resolution order:

1. The GTF recorded as `RunRole.ANNOTATION` on the run that produced this BAM,
   when present — the STAR index path (`--sjdbGTFfile`) already records this.
2. Otherwise, GTF objects in the project, offered for the user to pick.

With zero GTFs present, the states to design for are:

- **No GTF in project** — the charts' section explains that these need a gene
  annotation and points at the existing download/import path. This is the
  common case today and must not read as a broken feature.
- **Exactly one** — preselected, named in the UI so the user can see what was
  used.
- **Several** — an explicit picker; do not guess.

The GTF's contigs must intersect the BAM's header contig names. A GTF using
`1,2,3` against a BAM using `chr1,chr2,chr3` is the classic silent failure —
it yields 100% intergenic with no error. **Detect a zero or near-zero contig
name overlap up front and fail the job with a message naming the mismatch,**
rather than storing a plausible-looking all-intergenic result.

## Computation

New handler alongside `run_bam_stats` in `backend/app/queue/align_handlers.py`,
with the parsing and math as pure functions in a new
`backend/app/pipelines/transcript_qc_runner.py` — mirroring the
`bam_stats_runner.py` split so the testable parts take strings and lists and
never touch the queue.

### Transcript model

Parse exons from the GTF, grouped by transcript. For each gene take **one
representative transcript** — the longest by summed exon length — rather than
averaging over all isoforms. Isoform averaging blurs the 3' signal the chart
exists to show, since isoforms of the same gene have different lengths and
their positions do not align.

Exclude transcripts shorter than a floor (~200 bp): normalizing a 90 bp
transcript into 100 bins produces noise, not signal.

### Gene body coverage

- 100 bins, 5'→3', per transcript, orientation-corrected by strand — a
  minus-strand transcript's 5' end is its highest coordinate, and skipping
  this inverts half the genes and flattens the curve to meaninglessness.
- Per bin, mean depth across transcripts; the curve is normalized so its
  maximum is 1.0, since absolute depth is already reported elsewhere and the
  question here is shape.
- Fact: `gene_body_coverage: [{"percentile": 0, "coverage": 0.42}, ...]`
  (100 entries).

### Feature distribution

Each primary, mapped, non-duplicate read is classified once by its alignment
start position:

- **Exonic** — overlaps any exon of any transcript.
- **Intronic** — within a gene's span but not in an exon.
- **Intergenic** — neither.

Exonic wins ties (a read overlapping an exon of one gene and an intron of
another counts exonic), so the categories are mutually exclusive and sum to
the classified total. Secondary/supplementary records are skipped, consistent
with `sequence_stats.py`.

- Fact: `feature_distribution: {"exonic": n, "intronic": n, "intergenic": n}`
  — raw counts, with percentages derived in the frontend.

### Sampling

Cap reads at `DEFAULT_SAMPLE_READS` (200,000), as `sequence_stats.py` does,
**but sample across the file rather than taking the head.** A
coordinate-sorted BAM's first 200k reads all come from the start of the first
contig; for gene body coverage that is a few hundred genes on one chromosome,
which is not a genome-wide answer. Iterate over transcripts and sample reads
per region, or stride across contigs proportionally to their length.

Record the read count actually used in the facts, and surface it in the UI, so
the chart states what it is based on.

> Note: this same head-of-file bias affects the already-shipped insert-size
> histogram, whose 200k reads come from one genomic region. Out of scope here;
> tracked separately.

## Frontend

New `TranscriptQc.tsx`, rendered from `BamResults.tsx` when the applicability
chain passes. Hand-rolled SVG, matching `CoverageChart.tsx` and
`SequenceCharts.tsx` — this repo has no charting library and adding one for
two charts is not justified.

- **Gene body coverage** — a line plot, x from 5' to 3', y normalized 0-1,
  axis labelled with the ends rather than percentiles alone.
- **Feature distribution** — a horizontal stacked bar with a legend and
  percentages. Preferred over a pie: three categories at very uneven
  proportions are easier to read and to compare between samples as a stacked
  bar, and it matches the existing visual language better than introducing the
  app's first pie chart.

Both state the GTF used and the number of reads sampled. Neither renders
absent its fact.

## Testing

Pure functions over small synthetic GTF and read fixtures:

- A minus-strand transcript produces a curve that is the mirror of the same
  reads on a plus-strand transcript — the orientation bug that would otherwise
  pass every symmetric fixture.
- Reads concentrated at the 3' end produce a monotonically rising curve.
- Uniform coverage produces a flat curve.
- Classification: a read fully inside an exon is exonic; inside a gene but
  between exons is intronic; outside all genes is intergenic; the three counts
  sum to the classified total.
- A read overlapping an exon of one gene and an intron of another is exonic.
- Transcripts under the length floor are excluded.
- Contig naming mismatch (`1` vs `chr1`) raises rather than returning
  all-intergenic.
- The applicability chain: each of its five branches, including that explicit
  `molecule_type: DNA` beats `aligned_by: star`.

Per CLAUDE.md's registry warning, this adds a job type and new facts: check
that any sidecar-role or suggestion mapping the new job touches actually has
an entry for it, and assert the *unavailable* direction in gating tests — the
image ships tools installed, so an availability assertion can pass whether or
not the patch worked.

Finally, verify against real objects rather than fixtures only. The four
STAR-aligned `ERR458*` RNA-seq BAMs in the current database are the intended
subject; a GTF must be imported first, which is itself the empty-state path
worth walking once by hand.

## Out of scope

- Populating `molecule_type` on existing objects. The chain above is designed
  to work without it; backfilling is a separate metadata concern.
- Matching RSeQC's numbers exactly (see above).
- Per-gene or per-transcript drill-down. These are whole-sample QC charts;
  anything finer belongs in a genome browser.
