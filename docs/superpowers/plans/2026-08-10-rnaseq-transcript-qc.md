# RNA-seq transcript QC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two on-demand RNA-seq alignment QC charts — gene body coverage (5'→3' bias) and genomic feature distribution (exonic/intronic/intergenic) — computed in one job from a BAM plus a GTF.

**Architecture:** One new job, `run_transcript_qc`, following the established
`run_bam_stats` split: pure functions in a new
`backend/app/pipelines/transcript_qc_runner.py` (GTF parsing, binning,
classification — testable over strings and lists), a thin handler in
`backend/app/queue/align_handlers.py` that does I/O and pysam traversal, a
facts applier in `backend/app/queue/results.py`, a launch path in
`backend/app/services/pipeline_service.py`, and a new `TranscriptQc.tsx`
rendered from `BamResults.tsx`. Both metrics come from one GTF parse and one
BAM pass but land as two independent facts, because their gating differs
(ChIP-seq gets feature distribution only).

**Tech Stack:** Python 3, pysam 0.24 (already installed), pytest, React +
TypeScript, hand-rolled SVG (no charting library in this repo).

**Spec:** [docs/superpowers/specs/2026-08-10-rnaseq-transcript-qc-design.md](../specs/2026-08-10-rnaseq-transcript-qc-design.md)
— Approved. Issues [#158](https://github.com/syntheticgio/bioflow/issues/158),
[#159](https://github.com/syntheticgio/bioflow/issues/159), epic
[#154](https://github.com/syntheticgio/bioflow/issues/154).

---

## Decisions this plan makes that the spec left open

The spec is approved and authoritative. Three points it under-specified are
resolved here, each grounded in code that already exists in this repo:

1. **Sampling strategy.** The spec said "iterate over transcripts and sample
   reads per region, **or** stride across contigs proportionally." This plan
   picks **stride across contigs proportionally to length**, mirroring
   `sequence_stats._fasta_sample_strided` (`backend/app/storage/sequence_stats.py:286`),
   which solves the identical head-of-file problem for FASTA and documents its
   own truncation failure mode. Per-transcript region fetches would issue
   hundreds of thousands of `fetch()` calls on a large annotation; striding is
   one linear pass with the same read budget.

2. **GTF vs GFF3 attribute names.** The spec says "parse exons from the GTF"
   without addressing format. `counts_runner.attributes_for_format`
   (`backend/app/pipelines/counts_runner.py:112`) already solved this
   empirically: NCBI GFF3 exon lines carry **no `gene_id` at all**, and
   `locus_tag` is the correct GFF3 grouping key (present on 100% of exon
   lines vs `gene`'s 84.5%). Task 2 reuses that function rather than
   reinventing it. Getting this wrong yields zero transcripts and a
   100%-intergenic chart — the same silent-failure shape the spec's contig
   check guards against.

3. **Interval overlap structure.** `intervaltree` is a declared dependency
   (`backend/pyproject.toml`) but is currently unused anywhere in `backend/app`.
   This plan uses plain sorted arrays plus `bisect` instead: exon lookup here
   is a static, build-once-query-many problem where a sorted array with
   binary search is both faster and dependency-free. Introducing the repo's
   first `intervaltree` usage for this is not justified.

---

## File Structure

**Create:**
- `backend/app/pipelines/transcript_qc_runner.py` — GTF parsing, transcript
  model, binning, read classification. Pure functions, no queue, no pysam.
- `backend/app/services/transcript_qc_gating.py` — the applicability chain and
  GTF resolution. Separate from the runner because it is a metadata question,
  not a computation, and it is consumed by both the API and the launch path.
- `backend/tests/pipelines/test_transcript_qc_runner.py`
- `backend/tests/services/test_transcript_qc_gating.py`
- `backend/tests/pipelines/test_transcript_qc_launch.py`
- `frontend/src/components/TranscriptQc.tsx`

**Modify:**
- `backend/app/queue/align_handlers.py` — add `run_transcript_qc` handler
  (append after `run_bam_stats`, which ends at :900).
- `backend/app/queue/results.py` — add `_apply_run_transcript_qc` and register
  it in the applier map at :2501.
- `backend/app/services/pipeline_service.py` — add `launch_transcript_qc`.
- `backend/app/api/v1/objects.py` — expose the gating decision + GTF choices.
- `frontend/src/api/types.ts` — facts types.
- `frontend/src/api/client.ts` — `launchTranscriptQc`.
- `frontend/src/components/BamResults.tsx` — render `TranscriptQc`.

---

## Task 1: Transcript model — parse exons from a GTF

**Files:**
- Create: `backend/app/pipelines/transcript_qc_runner.py`
- Create: `backend/tests/pipelines/test_transcript_qc_runner.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/pipelines/test_transcript_qc_runner.py`:

```python
from app.pipelines import transcript_qc_runner as tq

# Two transcripts of one gene, plus a second gene. Tab-separated, GTF spec
# order: seqname source feature start end score strand frame attributes.
# GTF coordinates are 1-based and inclusive on both ends.
GTF = "\n".join(
    [
        '# a comment line, which GTF permits and parsers must skip',
        'chr1\tsrc\texon\t1001\t1300\t.\t+\t.\tgene_id "G1"; transcript_id "T1";',
        'chr1\tsrc\texon\t2001\t2400\t.\t+\t.\tgene_id "G1"; transcript_id "T1";',
        # Shorter isoform of the same gene -- must lose to T1.
        'chr1\tsrc\texon\t1001\t1100\t.\t+\t.\tgene_id "G1"; transcript_id "T2";',
        'chr1\tsrc\tCDS\t1001\t1300\t.\t+\t.\tgene_id "G1"; transcript_id "T1";',
        'chr2\tsrc\texon\t500\t900\t.\t-\t.\tgene_id "G2"; transcript_id "T3";',
    ]
)


def test_parse_groups_exons_by_transcript_and_keeps_longest_per_gene():
    model = tq.parse_gtf(GTF.splitlines(), feature="exon", group_key="gene_id")

    # One representative transcript per gene, not per transcript_id.
    assert {t.gene_id for t in model.transcripts} == {"G1", "G2"}

    t1 = next(t for t in model.transcripts if t.gene_id == "G1")
    # T1 (300 + 400 = 700 bp) beats T2 (100 bp).
    assert t1.transcript_id == "T1"
    assert t1.exons == [(1000, 1300), (2000, 2400)]  # half-open, 0-based
    assert t1.length == 700
    assert t1.strand == "+"
    assert t1.contig == "chr1"

    t3 = next(t for t in model.transcripts if t.gene_id == "G2")
    assert t3.strand == "-"
    assert t3.length == 400


def test_parse_ignores_non_exon_features():
    model = tq.parse_gtf(GTF.splitlines(), feature="exon", group_key="gene_id")
    t1 = next(t for t in model.transcripts if t.gene_id == "G1")
    # The CDS line duplicates the first exon's span; counting it would
    # double that region's length.
    assert t1.length == 700


def test_parse_reads_gff3_style_attributes():
    # NCBI GFF3 exon lines carry no gene_id -- see
    # counts_runner.attributes_for_format. Grouping key is locus_tag there.
    gff = (
        'chr1\tsrc\texon\t1\t300\t.\t+\t.\t'
        'ID=exon-NM_1-1;Parent=rna-NM_1;gene=PAU8;locus_tag=YAL068C;'
    )
    model = tq.parse_gtf([gff], feature="exon", group_key="locus_tag")
    assert [t.gene_id for t in model.transcripts] == ["YAL068C"]


def test_parse_skips_transcripts_under_the_length_floor():
    short = 'chr1\tsrc\texon\t1\t120\t.\t+\t.\tgene_id "S"; transcript_id "S1";'
    model = tq.parse_gtf([short], feature="exon", group_key="gene_id")
    # 120 bp normalized into 100 bins is noise, not signal.
    assert model.transcripts == []


def test_parse_ignores_malformed_and_short_lines():
    lines = [
        "not a gtf line at all",
        'chr1\tsrc\texon\tNOT_AN_INT\t300\t.\t+\t.\tgene_id "X";',
        'chr1\tsrc\texon\t1001\t1900\t.\t+\t.\tgene_id "OK"; transcript_id "T";',
    ]
    model = tq.parse_gtf(lines, feature="exon", group_key="gene_id")
    assert [t.gene_id for t in model.transcripts] == ["OK"]
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker compose exec api python -m pytest tests/pipelines/test_transcript_qc_runner.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.pipelines.transcript_qc_runner'`

- [ ] **Step 3: Write the implementation**

Create `backend/app/pipelines/transcript_qc_runner.py`:

```python
"""Gene body coverage and genomic feature distribution for RNA-seq BAMs.

Kept separate from the job handler so the parts worth testing -- GTF parsing,
transcript selection, bin math, read classification -- are pure functions over
strings and lists, with no queue, no filesystem, and no pysam involved.
Mirrors bam_stats_runner.py's split for the same reason.

Numbers here are deliberately not expected to match RSeQC's to the decimal:
bin counts and tie-breaking are implementation choices. These charts are read
for shape -- is there a 3' cliff, is the exonic fraction dominant -- not for a
figure quoted in a methods section.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass, field

# 100 bins gives one point per percentile of transcript length: fine enough to
# show a 3' cliff, coarse enough that a single 200 bp transcript does not
# contribute a spike to every bin.
BIN_COUNT = 100

# Normalizing a transcript shorter than this into BIN_COUNT bins produces
# noise -- several bins would share one base, and one read would swing the
# whole curve.
MIN_TRANSCRIPT_LENGTH = 200

# GTF: gene_id "G1";   GFF3: locus_tag=YAL068C;
_GTF_ATTR = re.compile(r'(\w+)\s+"([^"]*)"')
_GFF3_ATTR = re.compile(r"(\w+)=([^;]*)")


@dataclass
class Transcript:
    """One gene's representative transcript, exons in ascending coordinate
    order, as 0-based half-open [start, end) intervals."""

    gene_id: str
    transcript_id: str
    contig: str
    strand: str
    exons: list[tuple[int, int]]

    @property
    def length(self) -> int:
        return sum(end - start for start, end in self.exons)

    @property
    def span(self) -> tuple[int, int]:
        """The gene body: first exon start to last exon end, introns included."""
        return (self.exons[0][0], self.exons[-1][1])


@dataclass
class TranscriptModel:
    transcripts: list[Transcript] = field(default_factory=list)

    @property
    def contigs(self) -> set[str]:
        return {t.contig for t in self.transcripts}


def parse_attributes(raw: str) -> dict[str, str]:
    """Column 9 of a GTF or GFF3 line, whichever style it is written in.

    Both are tried because a project can hold either; see
    counts_runner.attributes_for_format for why the *key* differs between them
    (NCBI GFF3 exon lines carry no gene_id at all).
    """
    attrs = {k: v for k, v in _GTF_ATTR.findall(raw)}
    if not attrs:
        attrs = {k: v for k, v in _GFF3_ATTR.findall(raw)}
    return attrs


def parse_gtf(
    lines,
    *,
    feature: str = "exon",
    group_key: str = "gene_id",
    min_length: int = MIN_TRANSCRIPT_LENGTH,
) -> TranscriptModel:
    """Build a one-transcript-per-gene model from GTF/GFF3 annotation lines.

    `feature` and `group_key` come from counts_runner.attributes_for_format so
    GTF and GFF3 are keyed by the attribute each actually carries.

    One representative transcript per gene -- the longest by summed exon
    length -- rather than an average over isoforms. Isoforms of one gene have
    different lengths and their positions do not align, so averaging them
    blurs the 3' signal the chart exists to show.
    """
    # (gene, transcript) -> exons, so isoforms stay separable until we choose.
    by_transcript: dict[tuple[str, str], Transcript] = {}

    for line in lines:
        if not line or line.startswith("#"):
            continue
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 9 or parts[2] != feature:
            continue

        attrs = parse_attributes(parts[8])
        gene = attrs.get(group_key)
        if not gene:
            continue
        # A GFF3 exon has no transcript_id; fall back to the gene so its exons
        # still group into one transcript rather than one per line.
        transcript = attrs.get("transcript_id") or attrs.get("Parent") or gene

        try:
            # GTF is 1-based inclusive; convert to 0-based half-open.
            start = int(parts[3]) - 1
            end = int(parts[4])
        except ValueError:
            continue
        if end <= start:
            continue

        key = (gene, transcript)
        existing = by_transcript.get(key)
        if existing is None:
            by_transcript[key] = Transcript(
                gene_id=gene,
                transcript_id=transcript,
                contig=parts[0],
                strand=parts[6] if parts[6] in {"+", "-"} else "+",
                exons=[(start, end)],
            )
        else:
            existing.exons.append((start, end))

    # Longest isoform wins, deterministically: ties break on transcript_id so
    # two runs over the same annotation always pick the same one.
    best: dict[str, Transcript] = {}
    for t in by_transcript.values():
        t.exons.sort()
        current = best.get(t.gene_id)
        if current is None or (t.length, t.transcript_id) > (
            current.length,
            current.transcript_id,
        ):
            best[t.gene_id] = t

    kept = [t for t in best.values() if t.length >= min_length]
    kept.sort(key=lambda t: (t.contig, t.span[0], t.gene_id))
    return TranscriptModel(transcripts=kept)
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
docker compose exec api python -m pytest tests/pipelines/test_transcript_qc_runner.py -q
```

Expected: PASS, 5 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/transcript_qc_runner.py backend/tests/pipelines/test_transcript_qc_runner.py
git commit -m "feat(pipelines): parse a one-transcript-per-gene model from GTF or GFF3"
```

---

## Task 2: Choose parsing keys by annotation format

**Files:**
- Modify: `backend/app/pipelines/transcript_qc_runner.py`
- Modify: `backend/tests/pipelines/test_transcript_qc_runner.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/pipelines/test_transcript_qc_runner.py`:

```python
def test_keys_for_format_reuses_the_counts_runner_decision():
    # Not a reimplementation: counts_runner measured this against the
    # annotations this app actually downloads (locus_tag on 100% of NCBI GFF3
    # exon lines, gene_id on 0%).
    assert tq.keys_for_format("gtf") == ("exon", "gene_id")
    assert tq.keys_for_format("gff") == ("exon", "locus_tag")
    assert tq.keys_for_format(None) == ("exon", "gene_id")
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker compose exec api python -m pytest tests/pipelines/test_transcript_qc_runner.py::test_keys_for_format_reuses_the_counts_runner_decision -q
```

Expected: FAIL — `AttributeError: module 'app.pipelines.transcript_qc_runner' has no attribute 'keys_for_format'`

- [ ] **Step 3: Write the implementation**

Add to `backend/app/pipelines/transcript_qc_runner.py`, after the imports:

```python
from app.pipelines.counts_runner import attributes_for_format


def keys_for_format(kind) -> tuple[str, str]:
    """The feature type and grouping attribute for an annotation format.

    Delegates to counts_runner rather than duplicating the mapping: that
    function's choice of `locus_tag` for GFF3 was measured against the files
    this application downloads, and a second copy of the rule here would be
    one more thing to keep in sync with it.
    """
    return attributes_for_format(kind)
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
docker compose exec api python -m pytest tests/pipelines/test_transcript_qc_runner.py -q
```

Expected: PASS, 6 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/transcript_qc_runner.py backend/tests/pipelines/test_transcript_qc_runner.py
git commit -m "feat(pipelines): key transcript parsing off the annotation format"
```

---

## Task 3: Gene body coverage binning, strand-corrected

**Files:**
- Modify: `backend/app/pipelines/transcript_qc_runner.py`
- Modify: `backend/tests/pipelines/test_transcript_qc_runner.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/pipelines/test_transcript_qc_runner.py`:

```python
def _transcript(strand: str, gene: str = "G") -> tq.Transcript:
    # One 1000 bp exon: transcript coordinate == genomic offset, so a test can
    # reason about positions directly.
    return tq.Transcript(
        gene_id=gene,
        transcript_id=f"{gene}-T",
        contig="chr1",
        strand=strand,
        exons=[(0, 1000)],
    )


def test_transcript_offset_maps_genomic_position_into_transcript_space():
    t = tq.Transcript(
        gene_id="G", transcript_id="T", contig="chr1", strand="+",
        exons=[(100, 200), (500, 600)],
    )
    assert tq.transcript_offset(t, 100) == 0     # first base of exon 1
    assert tq.transcript_offset(t, 199) == 99    # last base of exon 1
    assert tq.transcript_offset(t, 500) == 100   # first base of exon 2
    assert tq.transcript_offset(t, 599) == 199
    assert tq.transcript_offset(t, 300) is None  # intronic, not in transcript


def test_uniform_coverage_gives_a_flat_curve():
    acc = tq.GeneBodyAccumulator()
    t = _transcript("+")
    for pos in range(0, 1000, 10):
        acc.add_read(t, pos, pos + 10)
    curve = acc.to_facts()
    assert len(curve) == tq.BIN_COUNT
    assert all(abs(p["coverage"] - 1.0) < 0.01 for p in curve)


def test_three_prime_pileup_gives_a_rising_curve():
    acc = tq.GeneBodyAccumulator()
    t = _transcript("+")
    # Reads only in the last 20% of the transcript: classic degradation.
    for pos in range(800, 1000, 10):
        acc.add_read(t, pos, pos + 10)
    curve = acc.to_facts()
    assert curve[0]["coverage"] == 0.0
    assert curve[-1]["coverage"] == 1.0
    assert curve[10]["coverage"] <= curve[90]["coverage"]


def test_minus_strand_curve_mirrors_the_plus_strand_curve():
    # The orientation bug that passes every symmetric fixture: a minus-strand
    # transcript's 5' end is its *highest* coordinate. Skipping the flip
    # inverts half the genes and flattens the averaged curve to meaningless.
    plus = tq.GeneBodyAccumulator()
    minus = tq.GeneBodyAccumulator()
    for pos in range(800, 1000, 10):
        plus.add_read(_transcript("+"), pos, pos + 10)
        minus.add_read(_transcript("-"), pos, pos + 10)

    plus_curve = [p["coverage"] for p in plus.to_facts()]
    minus_curve = [p["coverage"] for p in minus.to_facts()]
    assert minus_curve == list(reversed(plus_curve))


def test_curve_is_normalized_to_a_maximum_of_one():
    acc = tq.GeneBodyAccumulator()
    t = _transcript("+")
    for _ in range(37):  # arbitrary depth
        for pos in range(0, 1000, 10):
            acc.add_read(t, pos, pos + 10)
    assert max(p["coverage"] for p in acc.to_facts()) == 1.0


def test_empty_accumulator_returns_no_curve():
    assert tq.GeneBodyAccumulator().to_facts() == []
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker compose exec api python -m pytest tests/pipelines/test_transcript_qc_runner.py -q -k "offset or curve or coverage or accumulator"
```

Expected: FAIL — `AttributeError: ... has no attribute 'transcript_offset'`

- [ ] **Step 3: Write the implementation**

Add to `backend/app/pipelines/transcript_qc_runner.py`:

```python
def transcript_offset(t: Transcript, genomic_pos: int) -> int | None:
    """Position within the spliced transcript, or None if not in an exon.

    Exons are sorted and non-overlapping, so a binary search over their
    cumulative lengths finds the containing exon without scanning them all --
    which matters when this runs once per sampled read against a transcript
    that can have dozens of exons.
    """
    starts = [start for start, _ in t.exons]
    idx = bisect_right(starts, genomic_pos) - 1
    if idx < 0:
        return None
    start, end = t.exons[idx]
    if not (start <= genomic_pos < end):
        return None
    preceding = sum(e - s for s, e in t.exons[:idx])
    return preceding + (genomic_pos - start)


class GeneBodyAccumulator:
    """Mean depth per percentile of transcript length, 5' to 3'.

    Accumulates across every sampled transcript so one lightly-covered gene
    does not dominate; the curve is normalized at the end because absolute
    depth is already reported by bam_stats and the question here is shape.
    """

    def __init__(self, bins: int = BIN_COUNT):
        self.bins = bins
        self._totals = [0.0] * bins

    def add_read(self, t: Transcript, ref_start: int, ref_end: int) -> None:
        """Add one read's overlap with a transcript, in transcript space."""
        length = t.length
        if length <= 0:
            return
        for pos in range(ref_start, ref_end):
            offset = transcript_offset(t, pos)
            if offset is None:
                continue
            # A minus-strand transcript is transcribed from its highest
            # coordinate down, so its 5' end is the far end of the array.
            if t.strand == "-":
                offset = length - 1 - offset
            idx = min(self.bins - 1, (offset * self.bins) // length)
            self._totals[idx] += 1.0

    def to_facts(self) -> list[dict]:
        peak = max(self._totals, default=0.0)
        if peak <= 0:
            return []
        return [
            {"percentile": i, "coverage": round(v / peak, 4)}
            for i, v in enumerate(self._totals)
        ]
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
docker compose exec api python -m pytest tests/pipelines/test_transcript_qc_runner.py -q
```

Expected: PASS, 12 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/transcript_qc_runner.py backend/tests/pipelines/test_transcript_qc_runner.py
git commit -m "feat(pipelines): bin gene body coverage 5' to 3', correcting for strand"
```

---

## Task 4: Classify reads as exonic, intronic, or intergenic

**Files:**
- Modify: `backend/app/pipelines/transcript_qc_runner.py`
- Modify: `backend/tests/pipelines/test_transcript_qc_runner.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/pipelines/test_transcript_qc_runner.py`:

```python
def _two_gene_model() -> tq.TranscriptModel:
    return tq.TranscriptModel(
        transcripts=[
            # Two exons with a 1000..2000 intron between them.
            tq.Transcript("G1", "T1", "chr1", "+", [(1000, 1500), (2000, 2500)]),
            tq.Transcript("G2", "T2", "chr1", "-", [(5000, 5800)]),
        ]
    )


def test_classify_read_inside_an_exon_is_exonic():
    idx = tq.FeatureIndex(_two_gene_model())
    assert idx.classify("chr1", 1100, 1200) == "exonic"


def test_classify_read_inside_a_gene_but_between_exons_is_intronic():
    idx = tq.FeatureIndex(_two_gene_model())
    assert idx.classify("chr1", 1600, 1700) == "intronic"


def test_classify_read_outside_all_genes_is_intergenic():
    idx = tq.FeatureIndex(_two_gene_model())
    assert idx.classify("chr1", 8000, 8100) == "intergenic"


def test_classify_read_on_an_unknown_contig_is_intergenic():
    idx = tq.FeatureIndex(_two_gene_model())
    assert idx.classify("chrZ", 1100, 1200) == "intergenic"


def test_exonic_wins_over_intronic_when_a_read_overlaps_both():
    # A read in G1's intron that also clips G3's exon counts exonic, so the
    # three categories stay mutually exclusive and sum to the classified total.
    model = tq.TranscriptModel(
        transcripts=[
            tq.Transcript("G1", "T1", "chr1", "+", [(1000, 1500), (2000, 2500)]),
            tq.Transcript("G3", "T3", "chr1", "+", [(1600, 1900)]),
        ]
    )
    assert tq.FeatureIndex(model).classify("chr1", 1550, 1650) == "exonic"


def test_counts_sum_to_the_classified_total():
    idx = tq.FeatureIndex(_two_gene_model())
    counts = tq.FeatureCounts()
    for start in (1100, 1600, 8000, 5100):
        counts.add(idx.classify("chr1", start, start + 50))
    facts = counts.to_facts()
    assert facts == {"exonic": 2, "intronic": 1, "intergenic": 1}
    assert sum(facts.values()) == counts.total == 4


def test_contig_overlap_detects_the_chr_prefix_mismatch():
    model = _two_gene_model()  # chr1
    # The classic silent failure: a GTF using 1,2,3 against a BAM using
    # chr1,chr2,chr3 yields 100% intergenic with no error anywhere.
    assert tq.contig_overlap(model, ["1", "2", "3"]) == 0
    assert tq.contig_overlap(model, ["chr1", "chr2"]) == 1
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker compose exec api python -m pytest tests/pipelines/test_transcript_qc_runner.py -q -k "classify or exonic or sum_to or contig_overlap"
```

Expected: FAIL — `AttributeError: ... has no attribute 'FeatureIndex'`

- [ ] **Step 3: Write the implementation**

Add to `backend/app/pipelines/transcript_qc_runner.py`:

```python
EXONIC = "exonic"
INTRONIC = "intronic"
INTERGENIC = "intergenic"


class FeatureIndex:
    """Per-contig sorted exon and gene-span arrays, queried by binary search.

    Sorted arrays rather than an interval tree: the model is built once and
    queried hundreds of thousands of times, exons within a contig are already
    disjoint after merging, and this keeps the dependency surface flat.
    """

    def __init__(self, model: TranscriptModel):
        exons: dict[str, list[tuple[int, int]]] = {}
        spans: dict[str, list[tuple[int, int]]] = {}
        for t in model.transcripts:
            exons.setdefault(t.contig, []).extend(t.exons)
            spans.setdefault(t.contig, []).append(t.span)
        self._exons = {c: _merge(v) for c, v in exons.items()}
        self._spans = {c: _merge(v) for c, v in spans.items()}

    def classify(self, contig: str, start: int, end: int) -> str:
        """Exonic if the read touches any exon, else intronic if it falls
        within any gene body, else intergenic.

        Exonic wins ties deliberately -- a read overlapping one gene's exon
        and another's intron is evidence of mature mRNA, which is what the
        chart is asking about.
        """
        if _overlaps(self._exons.get(contig), start, end):
            return EXONIC
        if _overlaps(self._spans.get(contig), start, end):
            return INTRONIC
        return INTERGENIC


def _merge(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sort and coalesce overlapping intervals so a later binary search can
    assume they are disjoint and ascending."""
    if not intervals:
        return []
    intervals = sorted(intervals)
    merged = [intervals[0]]
    for start, end in intervals[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _overlaps(intervals: list[tuple[int, int]] | None, start: int, end: int) -> bool:
    if not intervals:
        return False
    starts = [s for s, _ in intervals]
    # The last interval beginning at or before the read's end; if that one
    # does not reach the read's start, no earlier one can either.
    idx = bisect_right(starts, end - 1) - 1
    if idx < 0:
        return False
    return intervals[idx][1] > start


class FeatureCounts:
    """Raw exonic/intronic/intergenic counts; percentages are the frontend's
    job, so the stored fact stays the measurement rather than a derived view."""

    def __init__(self):
        self._counts = {EXONIC: 0, INTRONIC: 0, INTERGENIC: 0}

    def add(self, category: str) -> None:
        if category in self._counts:
            self._counts[category] += 1

    @property
    def total(self) -> int:
        return sum(self._counts.values())

    def to_facts(self) -> dict:
        return dict(self._counts)


def contig_overlap(model: TranscriptModel, bam_contigs) -> int:
    """How many contig names the annotation and the BAM share.

    Zero means the two use different naming conventions (`1` vs `chr1`), which
    would otherwise produce a confident, entirely wrong 100%-intergenic
    result. Callers must fail rather than store that.
    """
    return len(model.contigs & set(bam_contigs))
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
docker compose exec api python -m pytest tests/pipelines/test_transcript_qc_runner.py -q
```

Expected: PASS, 19 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/transcript_qc_runner.py backend/tests/pipelines/test_transcript_qc_runner.py
git commit -m "feat(pipelines): classify reads as exonic, intronic, or intergenic"
```

---

## Task 5: Stride the read sample across contigs

**Files:**
- Modify: `backend/app/pipelines/transcript_qc_runner.py`
- Modify: `backend/tests/pipelines/test_transcript_qc_runner.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/pipelines/test_transcript_qc_runner.py`:

```python
def test_sample_plan_allocates_reads_proportionally_to_contig_length():
    plan = tq.sample_plan(
        contig_lengths=[("chr1", 900_000), ("chr2", 100_000)],
        max_reads=100_000,
    )
    assert dict(plan) == {"chr1": 90_000, "chr2": 10_000}


def test_sample_plan_gives_every_contig_at_least_one_read():
    # A 500 bp plasmid alongside a 3 Gb genome rounds to zero reads and
    # vanishes from the chart entirely -- the same "small contigs never
    # disappear" rule bam_stats_runner.bin_depth follows.
    plan = dict(tq.sample_plan(
        contig_lengths=[("chr1", 3_000_000_000), ("plasmid", 500)],
        max_reads=1000,
    ))
    assert plan["plasmid"] >= 1


def test_sample_plan_handles_a_single_contig():
    assert dict(tq.sample_plan([("chr1", 1000)], max_reads=50)) == {"chr1": 50}


def test_sample_plan_is_empty_for_no_contigs():
    assert tq.sample_plan([], max_reads=1000) == []


def test_sample_plan_ignores_zero_length_contigs():
    plan = dict(tq.sample_plan([("chr1", 1000), ("empty", 0)], max_reads=100))
    assert "empty" not in plan
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker compose exec api python -m pytest tests/pipelines/test_transcript_qc_runner.py -q -k sample_plan
```

Expected: FAIL — `AttributeError: ... has no attribute 'sample_plan'`

- [ ] **Step 3: Write the implementation**

Add to `backend/app/pipelines/transcript_qc_runner.py`:

```python
# Matches sequence_stats.DEFAULT_SAMPLE_READS: the same budget the insert-size
# and MAPQ histograms spend, so this job's cost is predictable next to them.
DEFAULT_SAMPLE_READS = 200_000


def sample_plan(contig_lengths, *, max_reads: int = DEFAULT_SAMPLE_READS):
    """Split the read budget across contigs in proportion to their length.

    A coordinate-sorted BAM's first 200k reads all come from the start of the
    first contig -- for gene body coverage that is a few hundred genes on one
    chromosome, which is not a genome-wide answer. Same problem, and the same
    fix, as sequence_stats._fasta_sample_strided.

    Every contig with any length gets at least one read so a short plasmid
    does not round away to nothing.
    """
    usable = [(name, length) for name, length in contig_lengths if length > 0]
    if not usable:
        return []

    total = sum(length for _, length in usable)
    plan = []
    for name, length in usable:
        share = max(1, (max_reads * length) // total)
        plan.append((name, share))
    return plan
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
docker compose exec api python -m pytest tests/pipelines/test_transcript_qc_runner.py -q
```

Expected: PASS, 24 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/transcript_qc_runner.py backend/tests/pipelines/test_transcript_qc_runner.py
git commit -m "feat(pipelines): stride the transcript QC read sample across contigs"
```

---

## Task 6: The applicability chain

**Files:**
- Create: `backend/app/services/transcript_qc_gating.py`
- Create: `backend/tests/services/test_transcript_qc_gating.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/test_transcript_qc_gating.py`:

```python
from app.services import transcript_qc_gating as gating


def _obj(*, molecule_type=None, assay=None, aligned_by=None):
    """The three fields the chain reads, shaped as an object document."""
    return {
        "metadata": {"molecule_type": molecule_type, "assay": assay},
        "facts": {"aligned_by": aligned_by},
    }


def test_molecule_type_rna_is_applicable():
    d = gating.decide(_obj(molecule_type="RNA"))
    assert d.gene_body is True
    assert d.feature_distribution is True
    assert d.reason == "molecule_type"


def test_explicit_dna_beats_a_splice_aware_aligner():
    # The branch that matters: an explicit DNA answer outranks inference. A
    # STAR-aligned DNA BAM must not render a gene body curve.
    d = gating.decide(_obj(molecule_type="DNA", aligned_by="star"))
    assert d.gene_body is False
    assert d.feature_distribution is False
    assert d.reason == "molecule_type"


def test_assay_rnaseq_is_applicable_when_molecule_type_is_absent():
    # This is the branch every BAM in the real database takes: molecule_type
    # is populated on 0 of 9, assay on 9 of 9.
    d = gating.decide(_obj(assay="RNA-seq"))
    assert d.gene_body is True
    assert d.feature_distribution is True
    assert d.reason == "assay"


def test_chipseq_enables_feature_distribution_only():
    # ChIP-seq is DNA: the exonic/intronic split is meaningful, a 5'->3'
    # transcript curve is not.
    d = gating.decide(_obj(assay="ChIP-seq"))
    assert d.gene_body is False
    assert d.feature_distribution is True
    assert d.reason == "assay"


def test_splice_aware_aligner_is_the_weakest_signal():
    for aligner in ("star", "hisat2"):
        d = gating.decide(_obj(aligned_by=aligner))
        assert d.gene_body is True
        assert d.feature_distribution is True
        assert d.reason == "aligner"


def test_dna_aligner_is_not_applicable():
    d = gating.decide(_obj(aligned_by="bwa-mem2"))
    assert d.gene_body is False
    assert d.feature_distribution is False
    assert d.reason == "none"


def test_nothing_known_is_not_applicable():
    d = gating.decide(_obj())
    assert d.applicable is False
    assert d.reason == "none"


def test_assay_outranks_the_aligner():
    d = gating.decide(_obj(assay="WGS", aligned_by="star"))
    assert d.applicable is False
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker compose exec api python -m pytest tests/services/test_transcript_qc_gating.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.transcript_qc_gating'`

- [ ] **Step 3: Write the implementation**

Create `backend/app/services/transcript_qc_gating.py`:

```python
"""Whether transcript QC applies to a BAM, and why.

Separate from the runner because this is a metadata question rather than a
computation, and it is read by both the API (to decide what the Results tab
offers) and the launch path (to refuse a job that cannot mean anything).

There is no stored RNA-vs-DNA flag on a BAM -- pipeline_service documents that
this "is not knowable from the bytes." What exists instead is a chain of
signals of decreasing confidence. Checked against the real database rather
than the schema, which overturned the obvious design: `molecule_type` is
populated on 0 of 9 BAMs and `assay` on 9 of 9, so gating on molecule_type
alone would ship a feature that never appears for anyone.
"""

from __future__ import annotations

from dataclasses import dataclass

SPLICE_AWARE_ALIGNERS = {"star", "hisat2"}


@dataclass(frozen=True)
class Applicability:
    gene_body: bool
    feature_distribution: bool
    # Which link in the chain decided: molecule_type, assay, aligner, or none.
    reason: str

    @property
    def applicable(self) -> bool:
        return self.gene_body or self.feature_distribution


_NOT_APPLICABLE = Applicability(False, False, "none")


def decide(obj) -> Applicability:
    """First hit wins, strongest signal first."""
    metadata = _get(obj, "metadata") or {}
    facts = _get(obj, "facts") or {}

    molecule_type = (_get(metadata, "molecule_type") or "").strip().upper()
    if molecule_type == "RNA":
        return Applicability(True, True, "molecule_type")
    if molecule_type in {"DNA", "OTHER"}:
        # An explicit answer outranks every inference below it.
        return Applicability(False, False, "molecule_type")

    assay = (_get(metadata, "assay") or "").strip().lower()
    if assay:
        if assay == "rna-seq":
            return Applicability(True, True, "assay")
        if assay == "chip-seq":
            # DNA, so the transcript curve is meaningless -- but the
            # exonic/intronic split is exactly what a ChIP experiment is read
            # for. This is the one place the two charts' gating differs, and
            # why the job writes two independent facts.
            return Applicability(False, True, "assay")
        # A known, non-RNA assay is an answer, not silence: do not fall
        # through to the aligner guess.
        return _NOT_APPLICABLE

    aligned_by = (_get(facts, "aligned_by") or "").strip().lower()
    if aligned_by in SPLICE_AWARE_ALIGNERS:
        # Weakest signal: a splice-aware aligner is evidence, not proof.
        return Applicability(True, True, "aligner")

    return _NOT_APPLICABLE


def _get(source, key):
    """Read a field from either a mapping or a model object."""
    if source is None:
        return None
    if isinstance(source, dict):
        return source.get(key)
    return getattr(source, key, None)
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
docker compose exec api python -m pytest tests/services/test_transcript_qc_gating.py -q
```

Expected: PASS, 8 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/transcript_qc_gating.py backend/tests/services/test_transcript_qc_gating.py
git commit -m "feat(services): decide transcript QC applicability from metadata, not aligner alone"
```

---

## Task 7: The job handler

**Files:**
- Modify: `backend/app/queue/align_handlers.py` (append after `run_bam_stats`, which ends at :900)

- [ ] **Step 1: Write the handler**

Append to `backend/app/queue/align_handlers.py`:

```python
@handler(
    "run_transcript_qc",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    # Heavier than run_bam_stats: the transcript model for a mammalian
    # annotation is held in memory for the whole BAM pass.
    resources=JobResources(cpu=1, mem_mb=2048, io=IoClass.LIGHT),
    max_attempts=2,
)
def run_transcript_qc(ctx: JobContext) -> dict:
    """Gene body coverage and genomic feature distribution for an RNA-seq BAM.

    Read-only: derives no files, returns two facts. One GTF parse and one
    strided BAM pass produce both charts, because they need the same expensive
    inputs and appear side by side.

    Applicability is decided before this is enqueued (see
    pipeline_service.launch_transcript_qc); a failure here is a real problem
    with the BAM or the annotation, not a missing precondition.
    """
    import pysam

    from app.pipelines import transcript_qc_runner as tq

    object_id = ctx.payload.get("object_id")
    if not object_id:
        raise PermanentError("run_transcript_qc requires an 'object_id'")

    work = _prepare_workdir(ctx, "transcript_qc")

    bam_name = Path(ctx.payload.get("bam_name") or "aligned.bam").name
    bam = work / bam_name
    bam.unlink(missing_ok=True)
    bam.symlink_to(_resolve_blob(ctx.payload, "bam"))

    bai = work / f"{bam_name}{aligners.BAI_SUFFIX}"
    bai.unlink(missing_ok=True)
    bai.symlink_to(_resolve_blob(ctx.payload, "bai"))

    gtf_path = _resolve_blob(ctx.payload, "gtf")

    ctx.progress(phase="annotation", pct=0.1, message="parsing the annotation")
    feature, group_key = tq.keys_for_format(ctx.payload.get("gtf_format"))
    with open(gtf_path, errors="replace") as fh:
        model = tq.parse_gtf(fh, feature=feature, group_key=group_key)

    if not model.transcripts:
        raise PermanentError(
            f"No usable transcripts found in {ctx.payload.get('gtf_name')!r}. "
            f"Expected {feature!r} lines carrying a {group_key!r} attribute."
        )

    with pysam.AlignmentFile(str(bam), "rb") as af:
        bam_contigs = list(af.references)
        contig_lengths = list(zip(af.references, af.lengths))

    # A GTF using 1,2,3 against a BAM using chr1,chr2,chr3 yields 100%
    # intergenic with no error anywhere. Fail loudly instead of storing a
    # confident, entirely wrong result.
    if tq.contig_overlap(model, bam_contigs) == 0:
        raise PermanentError(
            "The annotation and the BAM share no contig names "
            f"(annotation: {sorted(model.contigs)[:3]}, "
            f"BAM: {bam_contigs[:3]}). They likely use different naming "
            "conventions, such as '1' versus 'chr1'."
        )

    ctx.progress(phase="classify", pct=0.3, message="classifying reads")
    index = tq.FeatureIndex(model)
    counts = tq.FeatureCounts()
    gene_body = tq.GeneBodyAccumulator()

    # Transcripts by contig, so each sampled read is matched only against the
    # genes that could possibly contain it.
    by_contig: dict[str, list] = {}
    for t in model.transcripts:
        by_contig.setdefault(t.contig, []).append(t)

    plan = tq.sample_plan(contig_lengths, max_reads=tq.DEFAULT_SAMPLE_READS)
    reads_used = 0

    with pysam.AlignmentFile(str(bam), "rb") as af:
        for contig, budget in plan:
            taken = 0
            for rec in af.fetch(contig):
                if taken >= budget:
                    break
                if rec.is_secondary or rec.is_supplementary:
                    continue
                if rec.is_unmapped or rec.is_duplicate:
                    continue

                start = rec.reference_start
                end = rec.reference_end
                if end is None:
                    continue

                counts.add(index.classify(contig, start, end))

                for t in by_contig.get(contig, ()):
                    span_start, span_end = t.span
                    if span_start <= start < span_end:
                        gene_body.add_read(t, start, end)
                        break

                taken += 1
                reads_used += 1

                if reads_used % 20_000 == 0:
                    ctx.progress(
                        phase="classify",
                        pct=min(0.9, 0.3 + 0.6 * reads_used / tq.DEFAULT_SAMPLE_READS),
                        message=f"classified {reads_used:,} reads",
                    )

    facts = {
        "transcript_qc_status": "ok",
        "transcript_qc_computed_at": datetime.now(UTC).isoformat(),
        "transcript_qc_gtf_name": ctx.payload.get("gtf_name"),
        "transcript_qc_reads_sampled": reads_used,
        "transcript_qc_transcripts": len(model.transcripts),
        # Two independent facts, not one blob: ChIP-seq gets the feature
        # distribution without the gene body curve.
        "feature_distribution": counts.to_facts(),
        # Absent rather than empty when no read landed in a transcript, so the
        # frontend can tell "not computed" from "measured as flat".
        **({"gene_body_coverage": gene_body.to_facts()} if gene_body.to_facts() else {}),
    }

    ctx.progress(phase="done", pct=1.0, message="transcript QC complete")
    log.info(
        "transcript_qc_finished",
        job_id=ctx.job_id,
        object_id=object_id,
        reads=reads_used,
        transcripts=len(model.transcripts),
    )

    return {
        "object_id": object_id,
        "project_id": ctx.payload.get("project_id"),
        "job_id": ctx.job_id,
        "facts": facts,
        "workdir": str(work),
    }
```

- [ ] **Step 2: Verify the handler registers**

```bash
docker compose restart worker && sleep 8 && docker compose logs worker --tail 40 | grep -i "handlers_loaded"
```

Expected: the `handlers_loaded` line includes `run_transcript_qc`.

Per CLAUDE.md: `worker` does not hot-reload, so this restart is required or
the job will not exist in the running process.

- [ ] **Step 3: Commit**

```bash
git add backend/app/queue/align_handlers.py
git commit -m "feat(pipelines): compute gene body coverage and feature distribution in one BAM pass"
```

---

## Task 8: Apply the facts

**Files:**
- Modify: `backend/app/queue/results.py` (applier near :1559, registry at :2501)

- [ ] **Step 1: Read the neighbouring applier**

```bash
sed -n '1559,1600p' backend/app/queue/results.py
```

Match its shape exactly — this repo's appliers share one structure, and the
registry at :2501 is a hand-maintained dict keyed by handler name. Per
CLAUDE.md's registry warning, a handler with no entry here **silently stores
nothing while the job reports success** — the exact failure that cost the
`build_index` job its eight index files.

- [ ] **Step 2: Add the applier**

Add to `backend/app/queue/results.py`, immediately after `_apply_run_bam_stats`:

```python
async def _apply_run_transcript_qc(result: dict, *, owner: str) -> None:
    """Merge gene body coverage and feature distribution onto the BAM.

    Read-only job: facts only, no derived objects and no sidecars, so there is
    no SidecarRole to register alongside this.
    """
    object_id = result.get("object_id")
    facts = result.get("facts") or {}
    if not object_id or not facts:
        return
    await _merge_facts(object_id, facts, owner=owner)
```

Adjust the final call to match whatever `_apply_run_bam_stats` actually uses
to merge facts — read it in Step 1 rather than assuming `_merge_facts`.

- [ ] **Step 3: Register it**

At `backend/app/queue/results.py:2501`, beside `"run_bam_stats"`:

```python
    "run_transcript_qc": _apply_run_transcript_qc,
```

- [ ] **Step 4: Verify the registration**

```bash
docker compose exec api python -c "
from app.queue import results
assert 'run_transcript_qc' in results._APPLIERS, 'applier not registered'
print('registered ok')
"
```

Expected: `registered ok`. Correct `_APPLIERS` to the map's real name if it differs.

- [ ] **Step 5: Commit**

```bash
git add backend/app/queue/results.py
git commit -m "feat(queue): merge transcript QC facts onto the aligned BAM"
```

---

## Task 9: The launch path

**Files:**
- Modify: `backend/app/services/pipeline_service.py`
- Create: `backend/tests/pipelines/test_transcript_qc_launch.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/pipelines/test_transcript_qc_launch.py`:

```python
import pytest

from app.services import transcript_qc_gating as gating


def test_gating_refuses_a_dna_bam():
    d = gating.decide({"metadata": {"molecule_type": "DNA"}, "facts": {}})
    assert not d.applicable


def test_gating_allows_a_star_aligned_bam():
    d = gating.decide({"metadata": {}, "facts": {"aligned_by": "star"}})
    assert d.applicable


def test_resolve_gtf_prefers_the_annotation_recorded_on_the_run():
    from app.services.transcript_qc_gating import resolve_gtf_choice

    chosen = resolve_gtf_choice(
        run_annotation_id="gtf-from-run",
        project_gtf_ids=["gtf-a", "gtf-b"],
    )
    assert chosen == "gtf-from-run"


def test_resolve_gtf_preselects_a_lone_project_gtf():
    from app.services.transcript_qc_gating import resolve_gtf_choice

    assert resolve_gtf_choice(run_annotation_id=None, project_gtf_ids=["only"]) == "only"


def test_resolve_gtf_refuses_to_guess_between_several():
    from app.services.transcript_qc_gating import resolve_gtf_choice

    assert resolve_gtf_choice(run_annotation_id=None, project_gtf_ids=["a", "b"]) is None


def test_resolve_gtf_is_none_when_the_project_has_none():
    from app.services.transcript_qc_gating import resolve_gtf_choice

    assert resolve_gtf_choice(run_annotation_id=None, project_gtf_ids=[]) is None
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker compose exec api python -m pytest tests/pipelines/test_transcript_qc_launch.py -q
```

Expected: FAIL — `ImportError: cannot import name 'resolve_gtf_choice'`

- [ ] **Step 3: Add the GTF resolver**

Add to `backend/app/services/transcript_qc_gating.py`:

```python
def resolve_gtf_choice(*, run_annotation_id, project_gtf_ids):
    """Which GTF to use, or None if the user must choose.

    The annotation recorded on the run that produced this BAM wins: a STAR
    index built with --sjdbGTFfile already names the exact annotation the
    alignment used, which is a fact rather than a guess. Failing that, a lone
    project GTF is preselected. Several means an explicit pick -- guessing
    between annotations silently changes the answer.
    """
    if run_annotation_id:
        return run_annotation_id
    if len(project_gtf_ids) == 1:
        return project_gtf_ids[0]
    return None
```

- [ ] **Step 4: Add the launch function**

Add to `backend/app/services/pipeline_service.py`, beside `launch_bam_stats` (:2044):

```python
async def launch_transcript_qc(
    *, object_id: PydanticObjectId, gtf_object_id: PydanticObjectId, owner: str
):
    """Queue gene body coverage and feature distribution for an RNA-seq BAM.

    On demand behind a button rather than automatic after alignment: the
    applicability chain is inference, and an automatic job on a mis-labelled
    DNA BAM burns a full pass to render a curve the user must learn to
    distrust.
    """
    from app.queue import queue
    from app.services import object_service, transcript_qc_gating

    bam = await object_service.get_object(object_id, owner=owner)
    _check_bam_stats_callable(bam)

    decision = transcript_qc_gating.decide(bam)
    if not decision.applicable:
        raise ValidationError(
            f"{bam.name!r} does not look like RNA-seq or ChIP-seq data, so "
            "transcript QC would not be meaningful."
        )

    bai = await _sidecar_of_role(bam, SidecarRole.BAI)
    if bai is None:
        raise ValidationError(
            f"{bam.name!r} has no index (.bai). Compute results first, which "
            "indexes the BAM."
        )

    gtf = await object_service.get_object(gtf_object_id, owner=owner)
    if not gtf.blob_sha256:
        raise ValidationError(f"{gtf.name!r} has no stored content yet")

    return await queue.enqueue(
        "run_transcript_qc",
        owner=owner,
        payload={
            "object_id": str(bam.id),
            "project_id": str(bam.project_id),
            "bam_name": bam.name,
            "bam": bam.blob_sha256,
            "bai": bai.blob_sha256,
            "gtf": gtf.blob_sha256,
            "gtf_name": gtf.name,
            "gtf_format": str(getattr(gtf.format, "kind", "") or ""),
        },
        job_class=JobClass.COMPUTE,
        resources=JobResources(cpu=1, mem_mb=2048, io=IoClass.LIGHT),
        max_attempts=2,
        dedup_key=f"transcript_qc:{bam.blob_sha256}:{gtf.blob_sha256}",
    )
```

Check the payload keys `bam`/`bai`/`gtf` against what `_resolve_blob` expects
by reading `launch_bam_stats`'s own enqueue call — match it rather than
assuming.

- [ ] **Step 5: Run the tests to verify they pass**

```bash
docker compose exec api python -m pytest tests/pipelines/test_transcript_qc_launch.py -q
```

Expected: PASS, 6 passed.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/app/services/transcript_qc_gating.py backend/tests/pipelines/test_transcript_qc_launch.py
git commit -m "feat(services): launch transcript QC against a chosen annotation"
```

---

## Task 10: The API endpoint

**Files:**
- Modify: `backend/app/api/v1/objects.py`

- [ ] **Step 1: Read the neighbouring endpoint**

```bash
grep -n "bam-stats\|launch_bam_stats" backend/app/api/v1/objects.py
```

Match its decorator, response shape, and owner handling.

- [ ] **Step 2: Add the endpoint**

Add to `backend/app/api/v1/objects.py`, beside the bam-stats route:

```python
@router.post("/{object_id}/transcript-qc")
async def launch_transcript_qc(
    object_id: PydanticObjectId,
    body: dict,
    owner: str = Depends(current_owner),
):
    """Queue transcript QC for a BAM against a chosen annotation."""
    gtf_object_id = body.get("gtf_object_id")
    if not gtf_object_id:
        raise HTTPException(status_code=400, detail="gtf_object_id is required")
    job = await pipeline_service.launch_transcript_qc(
        object_id=object_id,
        gtf_object_id=PydanticObjectId(gtf_object_id),
        owner=owner,
    )
    return {"job_id": str(job.id)}
```

Match the surrounding routes' owner dependency and error idiom — read Step 1's
output rather than assuming `current_owner` and `HTTPException` are what this
file uses.

- [ ] **Step 3: Verify the route registers**

```bash
docker compose exec api python -c "
from app.main import app
paths = [r.path for r in app.routes]
assert any('transcript-qc' in p for p in paths), 'route missing'
print('route ok')
"
```

Expected: `route ok`

- [ ] **Step 4: Commit**

```bash
git add backend/app/api/v1/objects.py
git commit -m "feat(api): expose a transcript QC launch endpoint"
```

---

## Task 11: Frontend types and client

**Files:**
- Modify: `frontend/src/api/types.ts`
- Modify: `frontend/src/api/client.ts`

- [ ] **Step 1: Add the types**

Add to `frontend/src/api/types.ts`, beside `BamStatsFacts`:

```typescript
export interface GeneBodyCoveragePoint {
  percentile: number;
  coverage: number;
}

export interface FeatureDistribution {
  exonic: number;
  intronic: number;
  intergenic: number;
}

export interface TranscriptQcFacts {
  transcript_qc_status?: "ok";
  transcript_qc_computed_at?: string;
  transcript_qc_gtf_name?: string;
  transcript_qc_reads_sampled?: number;
  transcript_qc_transcripts?: number;
  gene_body_coverage?: GeneBodyCoveragePoint[];
  feature_distribution?: FeatureDistribution;
}
```

- [ ] **Step 2: Add the client call**

Add to `frontend/src/api/client.ts`, beside `launchBamStats`:

```typescript
  launchTranscriptQc: (objectId: string, gtfObjectId: string) =>
    post(`/objects/${objectId}/transcript-qc`, { gtf_object_id: gtfObjectId }),
```

Match `launchBamStats`'s exact request helper and path prefix — read it first.

- [ ] **Step 3: Verify it typechecks**

```bash
docker compose exec web npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/api/client.ts
git commit -m "feat(frontend): type the transcript QC facts and launch call"
```

---

## Task 12: The charts

**Files:**
- Create: `frontend/src/components/TranscriptQc.tsx`

- [ ] **Step 1: Write the component**

Create `frontend/src/components/TranscriptQc.tsx`:

```typescript
import type {
  FeatureDistribution,
  GeneBodyCoveragePoint,
  TranscriptQcFacts,
} from "../api/types";

/**
 * RNA-seq transcript QC: 5'->3' coverage bias and where reads land relative
 * to genes.
 *
 * Hand-rolled SVG, matching CoverageChart.tsx and SequenceCharts.tsx -- this
 * repo has no charting library and two charts do not justify adding one.
 *
 * Both state the annotation used and the number of reads sampled: these are
 * sampled measurements, and a chart that hides that invites over-reading.
 */

export function GeneBodyCoverageChart({
  curve,
}: {
  curve: GeneBodyCoveragePoint[];
}) {
  if (!curve?.length) return null;

  const w = 360;
  const h = 180;
  const pad = { top: 12, right: 12, bottom: 28, left: 40 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;

  const x = (i: number) => pad.left + (i / (curve.length - 1)) * plotW;
  const y = (v: number) => pad.top + plotH - v * plotH;

  const path = curve
    .map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(p.coverage).toFixed(1)}`)
    .join(" ");

  return (
    <div>
      <div className="section-title">Gene body coverage</div>
      <svg width={w} height={h} role="img" aria-label="Gene body coverage curve">
        <line
          x1={pad.left} y1={pad.top + plotH} x2={pad.left + plotW} y2={pad.top + plotH}
          stroke="var(--border)"
        />
        <line
          x1={pad.left} y1={pad.top} x2={pad.left} y2={pad.top + plotH}
          stroke="var(--border)"
        />
        <path d={path} fill="none" stroke="var(--accent)" strokeWidth={2} />
        <text x={pad.left} y={h - 8} fontSize={11} fill="var(--text-faint)">
          5′
        </text>
        <text x={pad.left + plotW} y={h - 8} fontSize={11} fill="var(--text-faint)" textAnchor="end">
          3′
        </text>
        <text x={pad.left - 6} y={pad.top + 4} fontSize={11} fill="var(--text-faint)" textAnchor="end">
          1.0
        </text>
        <text x={pad.left - 6} y={pad.top + plotH} fontSize={11} fill="var(--text-faint)" textAnchor="end">
          0
        </text>
      </svg>
      <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
        Normalized mean coverage along the transcript. A sharp rise toward 3′
        suggests RNA degradation before sequencing.
      </div>
    </div>
  );
}

const CATEGORIES = [
  { key: "exonic", label: "Exonic", color: "var(--accent)" },
  { key: "intronic", label: "Intronic", color: "var(--warn)" },
  { key: "intergenic", label: "Intergenic", color: "var(--text-faint)" },
] as const;

export function FeatureDistributionChart({
  distribution,
}: {
  distribution: FeatureDistribution;
}) {
  const total =
    distribution.exonic + distribution.intronic + distribution.intergenic;
  if (!total) return null;

  const w = 360;
  const barH = 28;

  let offset = 0;
  const segments = CATEGORIES.map((c) => {
    const value = distribution[c.key];
    const width = (value / total) * w;
    const seg = { ...c, value, width, x: offset, pct: (value / total) * 100 };
    offset += width;
    return seg;
  });

  return (
    <div>
      <div className="section-title">Genomic feature distribution</div>
      {/* A stacked bar rather than a pie: three categories at very uneven
          proportions are easier to read and to compare between samples, and
          it matches the existing visual language. */}
      <svg width={w} height={barH} role="img" aria-label="Read distribution across genomic features">
        {segments.map((s) => (
          <rect key={s.key} x={s.x} y={0} width={Math.max(0, s.width)} height={barH} fill={s.color} />
        ))}
      </svg>
      <div style={{ display: "flex", gap: 16, flexWrap: "wrap", marginTop: 8 }}>
        {segments.map((s) => (
          <div key={s.key} style={{ fontSize: 12 }}>
            <span
              style={{
                display: "inline-block",
                width: 10,
                height: 10,
                background: s.color,
                marginRight: 6,
              }}
            />
            {s.label} {s.pct.toFixed(1)}%{" "}
            <span style={{ color: "var(--text-faint)" }}>
              ({s.value.toLocaleString()})
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

export function TranscriptQc({
  facts,
  showGeneBody,
}: {
  facts: TranscriptQcFacts;
  showGeneBody: boolean;
}) {
  if (facts.transcript_qc_status !== "ok") return null;

  const curve = facts.gene_body_coverage;
  const distribution = facts.feature_distribution;

  return (
    <div className="section">
      <div style={{ display: "flex", gap: 32, flexWrap: "wrap" }}>
        {showGeneBody && curve && curve.length > 0 && (
          <GeneBodyCoverageChart curve={curve} />
        )}
        {distribution && <FeatureDistributionChart distribution={distribution} />}
      </div>
      <div style={{ color: "var(--text-faint)", fontSize: 12, marginTop: 10 }}>
        Based on {(facts.transcript_qc_reads_sampled ?? 0).toLocaleString()}{" "}
        sampled reads across{" "}
        {(facts.transcript_qc_transcripts ?? 0).toLocaleString()} transcripts
        {facts.transcript_qc_gtf_name ? ` from ${facts.transcript_qc_gtf_name}` : ""}.
      </div>
    </div>
  );
}
```

Check `var(--accent)`, `var(--warn)`, `var(--border)`, and `var(--text-faint)`
exist in this repo's CSS variables; substitute the real names if not:

```bash
grep -rn "\-\-accent\|\-\-warn\|\-\-border\|\-\-text-faint" frontend/src/index.css | head
```

- [ ] **Step 2: Verify it typechecks**

```bash
docker compose exec web npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/TranscriptQc.tsx
git commit -m "feat(ui): chart gene body coverage and genomic feature distribution"
```

---

## Task 13: Wire the charts into the Results tab

**Files:**
- Modify: `frontend/src/components/BamResults.tsx`

- [ ] **Step 1: Add the import and the gated section**

Add the import beside the others at the top of `frontend/src/components/BamResults.tsx`:

```typescript
import { TranscriptQc } from "./TranscriptQc";
```

Inside the `hasResults && (...)` block, after the existing charts, add:

```typescript
          <TranscriptQc facts={obj.facts} showGeneBody={rnaApplicable} />
```

And above the `return`, derive the gate. This mirrors the backend chain in
`transcript_qc_gating.decide` — keep the two in step:

```typescript
  const moleculeType = (obj.metadata?.molecule_type ?? "").toUpperCase();
  const assay = (obj.metadata?.assay ?? "").toLowerCase();
  const alignedBy = (obj.facts.aligned_by ?? "").toLowerCase();

  const rnaApplicable =
    moleculeType === "RNA" ||
    (moleculeType !== "DNA" &&
      moleculeType !== "OTHER" &&
      (assay === "rna-seq" ||
        (!assay && (alignedBy === "star" || alignedBy === "hisat2"))));

  const featureApplicable = rnaApplicable || assay === "chip-seq";
```

- [ ] **Step 2: Add the compute button for the empty state**

Inside the `hasResults` block, before `<TranscriptQc .../>`:

```typescript
          {featureApplicable && obj.facts.transcript_qc_status !== "ok" && (
            <div className="section">
              <div className="section-title">RNA-seq transcript QC</div>
              {gtfChoices.length === 0 ? (
                <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
                  These charts need a gene annotation (GTF or GFF3) in this
                  project. Import or download one, then compute.
                </div>
              ) : (
                <>
                  <div style={{ color: "var(--text-faint)", fontSize: 12, marginBottom: 8 }}>
                    Coverage bias along transcripts and where reads land
                    relative to genes — computed on demand.
                  </div>
                  {gtfChoices.length > 1 && (
                    <select
                      value={gtfId ?? ""}
                      onChange={(e) => setGtfId(e.target.value)}
                      style={{ marginRight: 8 }}
                    >
                      <option value="">Choose an annotation…</option>
                      {gtfChoices.map((g) => (
                        <option key={g.id} value={g.id}>{g.name}</option>
                      ))}
                    </select>
                  )}
                  <button
                    type="button"
                    className="btn"
                    onClick={() => gtfId && computeTranscriptQc.mutate(gtfId)}
                    disabled={!gtfId || computeTranscriptQc.isPending}
                  >
                    {computeTranscriptQc.isPending ? "Computing…" : "Compute transcript QC"}
                  </button>
                </>
              )}
            </div>
          )}
```

With this state and mutation added beside the existing `compute` mutation:

```typescript
  const gtfChoices = (obj.project_annotations ?? []) as { id: string; name: string }[];
  const [gtfId, setGtfId] = useState<string | null>(
    gtfChoices.length === 1 ? gtfChoices[0].id : null,
  );

  const computeTranscriptQc = useMutation({
    mutationFn: (gtf: string) => api.launchTranscriptQc(obj.id, gtf),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      notify.info("Computing transcript QC");
    },
    onError: (e: Error) => notify.error(e.message),
  });
```

`obj.project_annotations` does not exist yet. Either add it to the object
detail response beside the existing facts, or fetch the project's GTF objects
with the existing objects query filtered to `format.kind in {gtf, gff}` —
check which pattern the neighbouring components use before choosing:

```bash
grep -rn "useQuery(\[\"objects\"" frontend/src/components/*.tsx | head -5
```

Add `useState` to the React import if it is not already there.

- [ ] **Step 3: Verify it typechecks and builds**

```bash
docker compose exec web npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/BamResults.tsx
git commit -m "feat(ui): offer transcript QC on RNA-seq alignments in the Results tab"
```

---

## Task 14: Full suite, then verify against the real database

**Files:** none — verification only.

Per CLAUDE.md: green unit tests are not sufficient here. The Actions-tab
suggestion rules passed a full green suite while being wrong about two things
that one look at a real project exposed. This job has the same shape: every
fixture above feeds it hand-built objects that already look the way the code
expects.

- [ ] **Step 1: Run the full backend suite**

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: all pass. **Read the count, not the exit code.** Compare the total
against the pre-change baseline; a suite that collects fewer tests than before
is a failure wearing a green hat. If it dies with `EXIT=137`, that is host
memory, not a test failure — rerun with fewer concurrent stacks.

- [ ] **Step 2: Confirm the worker loaded the handler**

```bash
docker compose up -d --build api web worker && sleep 12 && docker compose logs worker --tail 40 | grep -i handlers_loaded
```

Expected: `run_transcript_qc` present. Without this the job silently never
runs — `worker` has no reload mechanism.

- [ ] **Step 3: Check the gating against real objects, not fixtures**

```bash
docker compose exec api python -c "
import asyncio
from app.db.client import connect_to_mongo
from app.models.object import StoredObject
from app.services import transcript_qc_gating as g

async def main():
    await connect_to_mongo()
    bams = await StoredObject.find({'format.kind': 'bam'}).to_list()
    print(f'{len(bams)} BAMs')
    for b in bams:
        d = g.decide(b)
        print(f'  {b.name[:45]:45} gene_body={d.gene_body!s:5} '
              f'feat={d.feature_distribution!s:5} via={d.reason}')

asyncio.run(main())
"
```

Expected: the four STAR-aligned `ERR458*` RNA-seq BAMs decide
`gene_body=True feat=True via=assay`; the WGS BAMs decide False. If every BAM
comes back False, the chain is reading the wrong field names — fix that before
going further, because the feature would ship invisible.

- [ ] **Step 4: Walk the empty state by hand**

The database has **zero GTF objects**, so this is the state every current user
is in. Open a STAR-aligned BAM's Results tab at http://localhost:5173 and
confirm the section reads as "needs an annotation," not as a broken feature.

- [ ] **Step 5: Import a GTF and run the job end to end**

Download or import a GTF for the reference those BAMs were aligned against,
then click Compute transcript QC. Confirm:
- the job completes and both charts render
- the exonic fraction dominates (it is RNA-seq; if it does not, suspect a
  contig-name mismatch that the guard should have caught)
- the reads-sampled and annotation-name line is populated
- the gene body curve is not flat-zero

- [ ] **Step 6: Verify the contig-mismatch guard fires**

The most valuable negative test, and the one no fixture proves. If an
Ensembl-style GTF (contigs `1`, `2`, `3`) is available for a BAM using
`chr1`, run against it and confirm the job **fails with the naming message**
rather than storing a 100%-intergenic result.

- [ ] **Step 7: Commit any fixes**

```bash
git add -A && git commit -m "fix(pipelines): correct transcript QC against real objects"
```

Skip if nothing needed fixing.

---

## Task 15: Close out the issues and open the PR

**Files:**
- Modify: `docs/TODO.md` / `docs/TODO-done.md` (only if an entry covers this work)

- [ ] **Step 1: Check whether a TODO entry covers this**

```bash
grep -n -i "transcript\|gene body\|exonic\|rna-seq" docs/TODO.md
```

If an entry exists, append ` — FIXED` to its heading, note what shipped and
what the implementation did differently from the spec, and move the whole
entry to `docs/TODO-done.md`. If nothing matches, skip — these are tracked as
GitHub issues, not TODO entries.

- [ ] **Step 2: Push and open the PR**

```bash
git push -u origin HEAD
```

```bash
gh pr create --base main --title "feat(pipelines): chart RNA-seq 5'→3' coverage bias and read distribution across gene features" --body "$(cat <<'EOF'
Adds the two RNA-seq alignment QC charts from epic #154: gene body coverage
(5'→3' bias, a degradation signal) and genomic feature distribution
(exonic/intronic/intergenic, a gDNA-contamination signal).

## Why one PR

Both metrics need the same two expensive things — a parsed transcript model
from a GTF and a pass over the BAM classifying reads against it. Splitting
them would parse the GTF twice and traverse the BAM twice for two charts that
render side by side. One job, one parse, one pass, two independent facts.

The facts stay separate because the gating differs: ChIP-seq is DNA, so it
gets the feature distribution but not the transcript curve.

## Notable decisions

- **Custom pysam, not RSeQC.** Matches how insert-size and MAPQ were actually
  built here (`sequence_stats.py`) and avoids a new system dependency plus its
  TOOL_META, suggestion wiring, and help-page entries. Numbers will not match
  RSeQC to the decimal; these charts are read for shape.
- **Gating is a chain, not a flag.** There is no stored RNA-vs-DNA field.
  Checking the real database rather than the schema overturned the obvious
  design: `molecule_type` is populated on 0 of 9 BAMs, `assay` on 9 of 9.
  Gating on `molecule_type` alone would have shipped a feature nobody could
  see.
- **Strided sampling.** A coordinate-sorted BAM's first 200k reads all come
  from one chromosome, which is not a genome-wide answer. Mirrors
  `_fasta_sample_strided`.
- **Contig-name mismatch fails loudly.** A GTF using `1,2,3` against a BAM
  using `chr1,chr2,chr3` otherwise yields a confident 100%-intergenic result
  with no error.

Closes #158
Closes #159

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Label the PR**

`.github/release.yml` categorizes notes by label, not by the title prefix, so
an unlabelled PR lands under "Other changes":

```bash
gh pr edit --add-label "type:feature" --add-label "area:pipelines" --add-label "area:frontend" --add-label "area:backend"
```

- [ ] **Step 4: Update the issues**

```bash
gh issue comment 158 --body "Implemented in the linked PR: gene body coverage computed with pysam over a one-transcript-per-gene model, strand-corrected, 100 bins, normalized to peak. Gated by the applicability chain in \`transcript_qc_gating.decide\`."
```

```bash
gh issue comment 159 --body "Implemented in the linked PR alongside #158, sharing one GTF parse and one BAM pass. Feature distribution is available for ChIP-seq as well as RNA-seq, which is why it is a separate fact from the gene body curve."
```

- [ ] **Step 5: Report the PR URL and stop**

Do not merge. Per CLAUDE.md the end state of this work is an open PR; the user
reviews and merges.

---

## Spec coverage check

| Spec section | Task |
|---|---|
| One spec, one job, two facts | 7 |
| Custom pysam, not RSeQC | 7 (no TOOL_META needed) |
| Applicability chain, 5 branches | 6 |
| ChIP-seq gets feature distribution only | 6, 13 |
| On demand, not automatic | 9, 13 |
| GTF selection: run role → single → picker | 9, 13 |
| No-GTF empty state reads as next action | 13, 14 |
| Contig-name overlap fails loudly | 4, 7, 14 |
| Representative transcript = longest isoform | 1 |
| Length floor ~200 bp | 1 |
| 100 bins, strand-corrected, peak-normalized | 3 |
| Exonic wins ties; categories sum to total | 4 |
| Skip secondary/supplementary/duplicate | 7 |
| Sample capped, strided, count recorded | 5, 7 |
| Line plot + stacked bar, hand-rolled SVG | 12 |
| Both state GTF and reads sampled | 12 |
| Neither renders absent its fact | 12 |
| Full test list from the spec | 1, 3, 4, 6 |
| Registry-audit warning | 8 |
| Verify against real objects | 14 |

Out of scope per the spec, and absent here by intent: backfilling
`molecule_type`, matching RSeQC's numbers, per-gene drill-down, and the
head-of-file bias in the existing insert-size histogram.
