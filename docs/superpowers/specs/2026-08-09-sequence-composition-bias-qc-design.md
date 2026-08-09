# Sequence Composition & Bias QC

Two charts for the reads QC tab: a per-sequence GC distribution and a per-base
N content curve. Both answer questions the existing per-position quality curve
cannot -- contamination and systemic composition bias in the first case, a
single failed sequencing cycle in the second.

## Why these two, and why now

The QC tab already renders base composition (a whole-file pie) and mean quality
per position. Neither shows the *distribution across reads*: a library that is
half E. coli and half human has a perfectly ordinary aggregate GC and two peaks
in its per-read distribution. Nor does either show N as a function of cycle,
where the interesting failure -- one cycle collapsing -- is invisible in an
aggregate that averages it against every healthy cycle.

## What already exists (verified, not assumed)

`fastq_stats` in `backend/app/storage/sequence_stats.py` already accumulates
`per_read_gc: list[float]`, one GC percentage per sampled read, and then
discards the list after reducing it to `gc_per_read_mean`. The histogram is a
change of accumulator, not a new pass over the data.

fastp's report-only JSON was inspected against the real tool in the project's
own image rather than from documentation:

```
read1_before_filtering.content_curves -> ['A', 'T', 'C', 'G', 'N', 'GC']
N:  [0, 0, 0, 0, 0.666667, 0.333333, 0, 0, 0, 0]
GC: [0.333333, 0.666667, ...]
```

Two findings from that run drive the design:

- **Values are fractions, not percentages.** They need scaling at parse time to
  match the percent convention `base_composition` and `gc_content_percent`
  already use.
- **`content_curves.GC` is GC per cycle position, not a per-sequence
  distribution.** fastp emits no per-sequence GC histogram at all. This is why
  the two charts have two different sources rather than one preferred source
  and one fallback -- each has exactly one place it can come from.

`parse_qc_facts` (`backend/app/pipelines/fastp_runner.py`) currently reads only
the `summary` block and drops `content_curves` entirely.

| Chart | Source | Why that source |
|---|---|---|
| Per-sequence GC | ingest `sequence_stats.py` | only source; data already computed and discarded |
| Per-base N | fastp `content_curves.N` | only source; ingest has no per-position sequence pass |

Adding a per-position sequence pass at ingest was considered and rejected: it
would put per-cycle base counting on the upload hot path for every FASTQ, to
duplicate a number fastp already computes over the whole file rather than a
200k-read sample.

## Backend: GC histogram

In `fastq_stats`, replace the `per_read_gc` list with a `Counter[int]` keyed on
the rounded integer GC percentage. Emit:

```python
gc_per_read_histogram: [{"gc_percent": int, "count": int}]  # sparse, 0-100
```

`gc_per_read_mean` keeps its current meaning and computation.

This is a net memory reduction: today a 200k-read sample holds a 200k-element
float list purely to average it.

`alignment_stats` is deliberately left unchanged. The same accumulator would
work there, but the reads QC tab is the subject here and per-read GC on an
alignment is a different question with a different audience.

## Backend: N curve

In `parse_qc_facts`, read `read1_before_filtering.content_curves.N`, scale to
percent, and emit:

```python
qc_n_per_position: [{"position": int, "percent": float}]  # 1-indexed
```

1-indexed to match `quality_per_position`, so both curves share an x-axis
convention and the two charts can be read against each other.

**Omitted entirely when every value is zero.** That is the common case for
clean Illumina data, and a flat line at zero is a chart that never says
anything. Absent means "nothing to report", consistent with how every other
block in `QcReport` self-suppresses.

Only read1 is parsed. A paired run's read2 curve is a separate question; a
second chart per file is worth having only once someone has a file where the
two differ.

## Backend: expected GC, a three-tier cascade

New `backend/app/services/expected_gc.py`, resolving in order:

1. **Measured from a reference in the project.** An object with
   `ObjectRole.REFERENCE` and `gc_content_percent` in its facts -- a real
   measurement `fasta_stats` already performs. Attributed by filename:
   *"expected 40.9%, measured from GRCh38.fa"*.
2. **A cited table of well-established genomes.** Roughly eight entries (human,
   mouse, E. coli, yeast, Arabidopsis, Drosophila, C. elegans, Plasmodium),
   each carrying its value *and the source the value came from*. Keyed on
   `normalize_organism`. Attributed: *"expected 40.9% for Homo sapiens (GRCh38,
   Ensembl)"*.
3. **Neither.** Returns `None`. The frontend omits the overlay and offers the
   fitted-normal checkbox instead.

`OrganismBlurb` was considered as a source for tier 2 and rejected. Its own
docstring says *"Nothing here is authoritative... it is page colour"* -- it is
AI-generated prose, and an LLM-recalled GC percentage rendered as an
authoritative reference curve is precisely the fabricated-value failure
CLAUDE.md warns about for tool licences and citations. Every tier-2 entry
carries a citation for the same reason the `TOOL_META` fields do: a wrong
number on a surface that reads as authoritative is worse than a blank.

Tier 2 reads `obj.metadata.organism`. That field is already defined and
user-editable -- `FieldDef("organism", "Organism", group="Sequences",
suggested=True)` in `backend/app/metadata/schemas.py`, the same metadata
surface `readQuality` already reads `assay` from -- so tier 2 is reachable
today and needs no new UI. It returns nothing when the field is unset or does
not normalize to a table entry, and the cascade falls to tier 3. Tier 2 is
expected to miss more often than it hits, since the field is optional and
usually blank; that is correct behaviour rather than a gap.

Tier 2 must not write to the field or infer it. An organism guessed from a
filename and then used to draw an authoritative reference curve is the same
fabrication risk the `OrganismBlurb` rejection above is about.

Surfaced on `ObjectDetail` as an optional block, computed in `get_object`
alongside `summary_fingerprint`:

```python
expected_gc: {"percent": float, "source": str, "attribution": str} | None
```

**To verify during implementation:** whether the project-scoped reference query
is cheap enough to sit on the detail path uncached. If it is not, the cache
belongs in the service, not the endpoint.

## Frontend: two charts

Both live in `frontend/src/components/SequenceCharts.tsx` as hand-rolled SVG,
following the existing convention there (the file's own note: two fixed simple
shapes, and the smallest charting dependency would outweigh the rest of the
bundle). Both mount in the `qc-charts` grid in `DetailPanel.tsx` and render
nothing when their facts are absent.

**`GcDistributionChart`.** The observed histogram as the primary line with the
mean marked. The expected-GC overlay is drawn from the cascade above and
labelled with its attribution, so the reference line always says where its
number came from. When the cascade returns nothing, a checkbox reading *overlay
normal distribution* draws a normal fitted to the observed mode and standard
deviation -- labelled as a fit to this data, not an expectation of it, because
that is what FastQC's equivalent curve is and mislabelling it invites reading
noise as contamination. Checkbox is transient component state; it resets on
navigation and needs no home in settings.

**`NContentChart`.** Percent N per cycle, x-axis matching `QualityChart`, with
a reference line at 5% -- the same threshold the grading rule uses, so the
chart and the grade cannot disagree about what counts as a spike.

## Grading

`frontend/src/lib/readQuality.ts` gains one rule:

> **Any single cycle above 5% N demotes the grade by one, and supersedes the
> aggregate >1% N rule.**

5% sits well clear of the sub-1% noise floor of clean Illumina data, and a
genuine cycle failure spikes far higher.

The supersession matters: a file with a 40% spike at one cycle will usually
also clear the aggregate 1% threshold, and without it both rules fire for what
is one defect, dropping the grade by two. The spike is the more specific
diagnosis, so when it fires the aggregate rule stands down.

The caveat names the cycle, because that is what makes it actionable:
*"Cycle 47 is 38% N; a specific cycle failed."*

GC still never affects the grade. Drawing an expected-GC curve does not change
that -- the curve is context for a human reading the chart, not a threshold.

## Help

`frontend/src/components/HelpCalculations.tsx` needs three edits, and this is
part of the work rather than a follow-up: the page documents the grade, and a
grading rule that only exists in code is one nobody can check.

- The spike rule joins *What lowers the grade*, stating the supersession.
- *What GC content does not do* (currently at line 89) is rewritten. Its claim
  -- expected GC is a property of the organism, so an unusual GC is not
  evidence of a problem without knowing the source -- stays true and stays the
  reason GC is ungraded. But it is no longer the whole story once the app draws
  an expected-GC curve, so the section gains what the cascade is and where each
  tier's number comes from.
- The two new charts get a line each saying what they measure and what sampling
  they rest on -- the GC histogram is a 200k-read sample, the N curve is
  fastp's whole-file count, and the difference is worth stating where the
  numbers are explained.

## Testing

- `sequence_stats`: histogram bins and sums correctly; `gc_per_read_mean`
  unchanged from current behaviour on the same input.
- `parse_qc_facts`: N curve scaled from fraction to percent; fact absent when
  the curve is all zeros; absent when `content_curves` is missing entirely.
- `expected_gc`: each tier resolves; tier order is respected when more than one
  could apply; returns None cleanly at the bottom.
- `readQuality`: spike demotes; spike supersedes the aggregate rule rather than
  stacking; neither fires on a clean file.
- The charts themselves are verified in the browser -- there is no headless
  component-testing setup in this repo and none is expected. From this
  worktree that is `./ops/worktree-up.sh`, serving localhost:5273.

Per CLAUDE.md, the GC cascade gets checked against a real project via
`docker compose exec api python -c ...` and not only against fixtures. The
failure shape to look for is the one the suggestion rules hit: hand-built
objects that already look the way the resolver expects, passing green while a
real project's reference -- a `protein.faa`, a duplicate assembly, a FASTA
with no computed facts -- resolves to something wrong.
