# RNA-seq Transcript QC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** For RNA-seq alignments, add a gene body coverage curve (5'→3' bias) and a genomic feature distribution (exonic / intronic / intergenic), computed in one job.

**Architecture:** A new on-demand job parses exons from a project GTF into a transcript model, then makes one strided pass over the BAM with pysam, accumulating both metrics simultaneously. Applicability is a fallback chain (`molecule_type` → `assay` → aligner) because the authoritative field is unpopulated on real data. Pure functions live in a new `transcript_qc_runner.py`; the handler mirrors `run_bam_stats`.

**Tech Stack:** Python 3, pysam (already a dependency), pytest, React 18 + TypeScript, hand-rolled SVG.

**Spec:** `docs/superpowers/specs/2026-08-10-rnaseq-transcript-qc-design.md`

**Issues:** Closes #158, closes #159.

---

## Background the engineer needs

**Why one job for two charts.** Both need the same two expensive things: a parsed transcript model from a GTF, and a pass over the BAM classifying reads against it. Separately, that means parsing the GTF twice and traversing the BAM twice for two charts that sit side by side.

**Why custom pysam and not RSeQC.** RSeQC is the reference implementation, but adding it means a system dependency plus a `TOOL_META` entry with license/citation/usage and its completeness test, plus `suggestion_service` wiring — for two curves that are a few dozen lines over pysam. It also matches precedent: the comparable insert-size and MAPQ histograms were built with pysam in `storage/sequence_stats.py`, not by adding `samtools stats`. Accepted consequence: numbers won't match RSeQC to the decimal. These charts are read for shape, and this plan fixes the binning choices so the shape is stable.

**The gating trap — read this before writing the chain.** Both issues say there's no stored RNA-vs-DNA flag. That's now half wrong: `molecule_type` (DNA/RNA/Other) **is** implemented, mapped from SRA's `LIBRARY_SOURCE` in `metadata/sra.py`. Gating on it is the obvious design and it is wrong. Querying the live database:

| field | populated on BAMs |
|---|---|
| `molecule_type` | **0 of 9** |
| `assay` | **9 of 9**, and discriminates correctly (`RNA-seq` on exactly the STAR-aligned objects) |

`molecule_type` only lands when an SRA record supplies it. Gating on it alone ships a feature **no current user can see**, with a fully green test suite. Hence the fallback chain in Task 3.

**There are also zero GTF objects in the database.** GTF availability is a live gate, and "no GTF" is the state every current user is in — it must read as a clear next action, not a broken feature.

**Running tests from this worktree.** Use `./backend/run-worktree-tests.sh`, never `docker compose exec api python -m pytest` — the `api` container bind-mounts the *main* checkout, so the latter silently tests main's code.

## File structure

| File | Responsibility |
|---|---|
| `backend/app/pipelines/transcript_qc_runner.py` (create) | GTF parsing, transcript model, both accumulators — all pure |
| `backend/tests/pipelines/test_transcript_qc_runner.py` (create) | Unit tests for the above |
| `backend/app/services/transcript_qc_gating.py` (create) | The applicability chain, pure and separately testable |
| `backend/tests/services/test_transcript_qc_gating.py` (create) | Chain tests, including the branch-precedence cases |
| `backend/app/queue/transcript_qc_handlers.py` (create) | The `run_transcript_qc` handler |
| `backend/app/queue/results.py` (modify) | Register `_apply_run_transcript_qc` |
| `backend/app/services/pipeline_service.py` (modify) | `launch_transcript_qc` |
| `backend/app/api/v1/pipelines.py` (modify) | `POST /pipelines/transcript-qc` |
| `frontend/src/api/types.ts`, `client.ts` (modify) | Types + launch call |
| `frontend/src/components/TranscriptQc.tsx` (create) | Both charts, gating states |
| `frontend/src/components/BamResults.tsx` (modify) | Render it |

---

### Task 1: Parse a GTF into a transcript model

**Files:**
- Create: `backend/app/pipelines/transcript_qc_runner.py`
- Test: `backend/tests/pipelines/test_transcript_qc_runner.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/pipelines/test_transcript_qc_runner.py`:

```python
"""Transcript-model parsing and RNA-seq QC accumulation.

Pure functions over strings and lists, mirroring bam_stats_runner.py: no
queue, no filesystem, no pysam objects.
"""

import pytest

from app.pipelines.transcript_qc_runner import (
    MIN_TRANSCRIPT_LENGTH,
    Transcript,
    parse_gtf_transcripts,
    representative_transcripts,
)

# Two transcripts of one gene, plus a minus-strand gene on another contig.
GTF = "\n".join(
    [
        '#!genome-build GRCh38',
        'chr1\tx\texon\t101\t200\t.\t+\t.\tgene_id "G1"; transcript_id "T1";',
        'chr1\tx\texon\t301\t400\t.\t+\t.\tgene_id "G1"; transcript_id "T1";',
        'chr1\tx\texon\t101\t150\t.\t+\t.\tgene_id "G1"; transcript_id "T2";',
        'chr2\tx\texon\t1001\t1300\t.\t-\t.\tgene_id "G2"; transcript_id "T3";',
        'chr1\tx\tCDS\t101\t200\t.\t+\t.\tgene_id "G1"; transcript_id "T1";',
    ]
)


class TestParseGtf:
    def test_groups_exons_by_transcript(self):
        ts = parse_gtf_transcripts(GTF.splitlines())
        assert {t.transcript_id for t in ts} == {"T1", "T2", "T3"}
        t1 = next(t for t in ts if t.transcript_id == "T1")
        assert t1.exons == [(101, 200), (301, 400)]
        assert t1.contig == "chr1"
        assert t1.strand == "+"
        assert t1.gene_id == "G1"

    def test_ignores_non_exon_features(self):
        """Counting a CDS as well as its exon would double-count those bases."""
        ts = parse_gtf_transcripts(GTF.splitlines())
        t1 = next(t for t in ts if t.transcript_id == "T1")
        assert len(t1.exons) == 2

    def test_skips_comments_and_blank_lines(self):
        ts = parse_gtf_transcripts(["", "# comment", "#!genome-build x"])
        assert ts == []

    def test_records_strand(self):
        ts = parse_gtf_transcripts(GTF.splitlines())
        assert next(t for t in ts if t.transcript_id == "T3").strand == "-"

    def test_transcript_length_is_summed_exon_length(self):
        ts = parse_gtf_transcripts(GTF.splitlines())
        t1 = next(t for t in ts if t.transcript_id == "T1")
        assert t1.length == 200  # 100 + 100


class TestRepresentativeTranscripts:
    def test_picks_the_longest_transcript_per_gene(self):
        """Averaging over isoforms blurs the 3' signal the chart exists to
        show: isoforms differ in length, so their positions do not align."""
        ts = representative_transcripts(parse_gtf_transcripts(GTF.splitlines()))
        by_gene = {t.gene_id: t.transcript_id for t in ts}
        assert by_gene["G1"] == "T1"  # 200 bp beats T2's 50 bp

    def test_drops_transcripts_below_the_length_floor(self):
        """Normalizing a very short transcript into 100 bins produces noise,
        not signal."""
        short = "\n".join(
            [
                f'c\tx\texon\t1\t{MIN_TRANSCRIPT_LENGTH - 1}\t.\t+\t.\t'
                'gene_id "S"; transcript_id "S1";'
            ]
        )
        assert representative_transcripts(parse_gtf_transcripts(short.splitlines())) == []
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_transcript_qc_runner.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.pipelines.transcript_qc_runner'`.

- [ ] **Step 3: Write the implementation**

Create `backend/app/pipelines/transcript_qc_runner.py`:

```python
"""Transcript-model parsing and RNA-seq QC accumulation.

Kept separate from the job handler so the parts worth testing -- GTF parsing,
representative-transcript choice, and both accumulators -- are pure functions
over strings and lists, with no queue, filesystem, or pysam involved. Mirrors
bam_stats_runner.py's split for the same reason.
"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

# Gene body coverage is reported at percentile resolution: 100 bins from the
# 5' end to the 3' end, so transcripts of wildly different lengths can be
# averaged onto one axis.
GENE_BODY_BINS = 100

# Below this, normalizing into GENE_BODY_BINS bins interpolates more than it
# measures -- several bins per base is noise, not signal.
MIN_TRANSCRIPT_LENGTH = 200


@dataclass
class Transcript:
    """One transcript's exon structure, in reference coordinates."""

    transcript_id: str
    gene_id: str
    contig: str
    strand: str
    exons: list[tuple[int, int]] = field(default_factory=list)

    @property
    def length(self) -> int:
        """Summed exon length -- the transcript's own length, not its genomic
        span, which would include introns."""
        return sum(end - start + 1 for start, end in self.exons)

    @property
    def span(self) -> tuple[int, int]:
        """First to last exon coordinate, introns included. This is the gene
        body used to classify a read as intronic."""
        return self.exons[0][0], self.exons[-1][1]


def _attribute(attrs: str, key: str) -> str | None:
    """Pull one value out of a GTF attribute column.

    The column is `key "value"; key "value";` -- parsed by scanning rather
    than with a regex so a missing or unquoted attribute yields None instead
    of raising.
    """
    for part in attrs.split(";"):
        part = part.strip()
        if not part.startswith(key):
            continue
        _, _, value = part.partition(" ")
        return value.strip().strip('"')
    return None


def parse_gtf_transcripts(lines: Iterable[str]) -> list[Transcript]:
    """Group a GTF's exon features by transcript.

    Only `exon` rows are read. A GTF repeats the same bases across `exon`,
    `CDS`, `start_codon` and more, so counting anything else would
    double-count them. Exons are sorted by coordinate, which the gene-body
    walk depends on.
    """
    by_id: dict[str, Transcript] = {}
    for line in lines:
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 9 or parts[2] != "exon":
            continue
        transcript_id = _attribute(parts[8], "transcript_id")
        gene_id = _attribute(parts[8], "gene_id")
        if not transcript_id or not gene_id:
            continue
        t = by_id.get(transcript_id)
        if t is None:
            t = Transcript(
                transcript_id=transcript_id,
                gene_id=gene_id,
                contig=parts[0],
                strand=parts[6],
            )
            by_id[transcript_id] = t
        t.exons.append((int(parts[3]), int(parts[4])))

    for t in by_id.values():
        t.exons.sort()
    return list(by_id.values())


def representative_transcripts(transcripts: list[Transcript]) -> list[Transcript]:
    """One transcript per gene: the longest by summed exon length.

    Averaging the gene-body curve over every isoform blurs exactly the signal
    it exists to show. Isoforms of one gene differ in length, so a given
    percentile is a different place in each -- the 3' cliff of a degraded
    sample smears across the axis. One representative per gene keeps each
    percentile meaning one thing.
    """
    best: dict[str, Transcript] = {}
    for t in transcripts:
        if t.length < MIN_TRANSCRIPT_LENGTH:
            continue
        current = best.get(t.gene_id)
        if current is None or t.length > current.length:
            best[t.gene_id] = t
    return list(best.values())
```

- [ ] **Step 4: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_transcript_qc_runner.py -q
```

Expected: PASS, 7 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/transcript_qc_runner.py backend/tests/pipelines/test_transcript_qc_runner.py
git commit -m "feat(pipelines): parse a GTF into per-gene representative transcripts"
```

---

### Task 2: The two accumulators

**Files:**
- Modify: `backend/app/pipelines/transcript_qc_runner.py`
- Test: `backend/tests/pipelines/test_transcript_qc_runner.py`

- [ ] **Step 1: Write the failing tests**

Append to the test file (add the new names to its import block):

```python
from app.pipelines.transcript_qc_runner import (  # noqa: E402
    FeatureCounts,
    GeneBodyCoverage,
    build_feature_index,
    classify_position,
    contig_overlap,
)


def _t(tid, contig, strand, exons, gene=None):
    return Transcript(
        transcript_id=tid, gene_id=gene or tid, contig=contig, strand=strand, exons=exons
    )


class TestGeneBodyCoverage:
    def test_uniform_coverage_gives_a_flat_curve(self):
        t = _t("T1", "chr1", "+", [(1, 1000)])
        g = GeneBodyCoverage()
        for pos in range(1, 1001):
            g.add_read(t, pos)
        curve = [p["coverage"] for p in g.to_facts()]
        assert max(curve) == 1.0
        assert min(curve) > 0.9  # flat within binning noise

    def test_three_prime_pileup_rises_toward_the_end(self):
        """The degraded-RNA signature: poly-A selection keeps only the 3'
        tail, so coverage climbs from 5' to 3'."""
        t = _t("T1", "chr1", "+", [(1, 1000)])
        g = GeneBodyCoverage()
        for pos in range(900, 1001):
            g.add_read(t, pos)
        curve = [p["coverage"] for p in g.to_facts()]
        assert curve[0] == 0.0
        assert curve[-1] == 1.0

    def test_minus_strand_transcripts_are_oriented_five_to_three(self):
        """A minus-strand transcript's 5' end is its *highest* coordinate.
        Skipping this inverts half the genes, and averaging the two opposing
        gradients flattens the curve into meaninglessness."""
        plus = _t("P", "chr1", "+", [(1, 1000)])
        minus = _t("M", "chr1", "-", [(1, 1000)])

        gp = GeneBodyCoverage()
        for pos in range(900, 1001):  # 3' end of a plus-strand transcript
            gp.add_read(plus, pos)

        gm = GeneBodyCoverage()
        for pos in range(1, 102):  # 3' end of a minus-strand transcript
            gm.add_read(minus, pos)

        assert [p["coverage"] for p in gp.to_facts()] == [
            p["coverage"] for p in gm.to_facts()
        ]

    def test_spliced_transcripts_measure_transcript_position_not_genomic(self):
        """Two exons with a large intron: a read at the start of exon 2 is at
        the transcript's midpoint, not at 90% of its genomic span."""
        t = _t("T1", "chr1", "+", [(1, 500), (9501, 10000)])
        g = GeneBodyCoverage()
        g.add_read(t, 9501)
        curve = [p["coverage"] for p in g.to_facts()]
        assert curve[50] == 1.0
        assert curve[90] == 0.0

    def test_curve_is_normalized_to_its_maximum(self):
        t = _t("T1", "chr1", "+", [(1, 1000)])
        g = GeneBodyCoverage()
        for pos in range(1, 1001):
            g.add_read(t, pos)
        assert max(p["coverage"] for p in g.to_facts()) == 1.0

    def test_emits_one_point_per_bin(self):
        t = _t("T1", "chr1", "+", [(1, 1000)])
        g = GeneBodyCoverage()
        g.add_read(t, 1)
        facts = g.to_facts()
        assert len(facts) == GENE_BODY_BINS
        assert facts[0]["percentile"] == 0
        assert facts[-1]["percentile"] == 99

    def test_no_reads_gives_an_empty_curve(self):
        assert GeneBodyCoverage().to_facts() == []


class TestFeatureClassification:
    TS = [
        _t("T1", "chr1", "+", [(101, 200), (301, 400)]),
        _t("T2", "chr2", "-", [(1001, 1100)]),
    ]

    def test_a_read_inside_an_exon_is_exonic(self):
        idx = build_feature_index(self.TS)
        assert classify_position(idx, "chr1", 150) == "exonic"

    def test_a_read_between_exons_of_one_gene_is_intronic(self):
        idx = build_feature_index(self.TS)
        assert classify_position(idx, "chr1", 250) == "intronic"

    def test_a_read_outside_every_gene_is_intergenic(self):
        idx = build_feature_index(self.TS)
        assert classify_position(idx, "chr1", 5000) == "intergenic"

    def test_a_read_on_an_unknown_contig_is_intergenic(self):
        idx = build_feature_index(self.TS)
        assert classify_position(idx, "chrUnplaced", 5) == "intergenic"

    def test_exonic_wins_when_a_position_is_both(self):
        """Overlapping genes on opposite strands are common. Exonic winning
        keeps the three categories mutually exclusive so they sum to the
        classified total."""
        overlapping = [
            _t("A", "chr1", "+", [(100, 200), (400, 500)], gene="GA"),
            _t("B", "chr1", "-", [(250, 260)], gene="GB"),
        ]
        idx = build_feature_index(overlapping)
        # 250 is inside GA's intron and inside GB's exon.
        assert classify_position(idx, "chr1", 250) == "exonic"

    def test_counts_sum_to_the_classified_total(self):
        c = FeatureCounts()
        c.add("exonic")
        c.add("intronic")
        c.add("intergenic")
        c.add("exonic")
        facts = c.to_facts()
        assert facts == {"exonic": 2, "intronic": 1, "intergenic": 1}
        assert sum(facts.values()) == c.total


class TestContigOverlap:
    def test_matching_names_overlap(self):
        assert contig_overlap({"chr1", "chr2"}, {"chr1", "chr3"}) == 1

    def test_ensembl_style_names_do_not_match_ucsc_style(self):
        """'1' vs 'chr1' is the classic silent failure: every read lands
        outside every gene and the result is a plausible-looking 100%
        intergenic with no error anywhere."""
        assert contig_overlap({"1", "2"}, {"chr1", "chr2"}) == 0
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_transcript_qc_runner.py -q
```

Expected: FAIL — `ImportError: cannot import name 'GeneBodyCoverage'`.

- [ ] **Step 3: Write the implementation**

Append to `backend/app/pipelines/transcript_qc_runner.py`:

```python
import bisect


class GeneBodyCoverage:
    """Mean coverage across transcript position, 5' to 3', over all genes.

    Each read contributes to the bin holding its position *within the
    transcript* -- spliced coordinates, so an intron never shifts a read
    toward the 3' end -- and minus-strand transcripts are flipped so every
    curve runs 5' to 3'. Without that flip half the genes run backwards and
    averaging the two opposing gradients flattens the result to a
    meaningless straight line.

    A curve that climbs steeply toward the 3' end is the signature of RNA
    degraded before sequencing: poly-A selection captures only the surviving
    3' tail.
    """

    def __init__(self, bins: int = GENE_BODY_BINS):
        self.bins = bins
        self._sums = [0.0] * bins

    def add_read(self, transcript: Transcript, position: int) -> None:
        offset = transcript_offset(transcript, position)
        if offset is None:
            return
        length = transcript.length
        if length <= 0:
            return
        fraction = offset / length
        if transcript.strand == "-":
            # The 5' end of a minus-strand transcript is its highest
            # coordinate, so the fraction measured in ascending coordinates
            # runs 3'->5' and has to be reversed.
            fraction = 1.0 - fraction
        idx = min(int(fraction * self.bins), self.bins - 1)
        self._sums[idx] += 1.0

    def to_facts(self) -> list[dict]:
        """Normalized to the curve's own maximum: absolute depth is reported
        elsewhere, and the question here is shape."""
        peak = max(self._sums, default=0.0)
        if peak <= 0:
            return []
        return [
            {"percentile": i, "coverage": round(v / peak, 4)}
            for i, v in enumerate(self._sums)
        ]


def transcript_offset(transcript: Transcript, position: int) -> int | None:
    """How far into the transcript a genomic position falls, in spliced
    coordinates. None when the position is not inside an exon.

    Genomic distance would be wrong for any spliced transcript: a read at the
    start of a final exon beyond a 9 kb intron is at the transcript's
    midpoint, not at 90% of its genomic span.
    """
    consumed = 0
    for start, end in transcript.exons:
        if start <= position <= end:
            return consumed + (position - start)
        consumed += end - start + 1
    return None


def build_feature_index(transcripts: list[Transcript]) -> dict:
    """Per-contig sorted exon and gene-span intervals, for classification.

    Sorted lists searched with bisect rather than a full interval tree: the
    per-contig lists are small enough that the log-n lookup is not the
    bottleneck (the BAM pass is), and there is no dependency to add.
    """
    exons: dict[str, list[tuple[int, int]]] = {}
    genes: dict[str, dict[str, tuple[int, int]]] = {}
    for t in transcripts:
        exons.setdefault(t.contig, []).extend(t.exons)
        start, end = t.span
        by_gene = genes.setdefault(t.contig, {})
        current = by_gene.get(t.gene_id)
        by_gene[t.gene_id] = (
            min(start, current[0]) if current else start,
            max(end, current[1]) if current else end,
        )

    return {
        "exons": {c: sorted(v) for c, v in exons.items()},
        "genes": {c: sorted(v.values()) for c, v in genes.items()},
    }


def _covers(intervals: list[tuple[int, int]], position: int) -> bool:
    """Whether any interval contains the position.

    Intervals can overlap, so the candidate found by bisect is not
    necessarily the containing one -- scan back while starts are still at or
    below the position.
    """
    i = bisect.bisect_right(intervals, (position, float("inf")))
    for start, end in reversed(intervals[:i]):
        if end >= position:
            return True
        # Sorted by start; a run of non-covering intervals can still be
        # followed by a long one that does, so only stop once starts are far
        # enough back that nothing can reach.
        if start < position - _MAX_FEATURE_SPAN:
            break
    return False


# Longest interval we scan back through in _covers. Human introns reach ~2 Mb;
# beyond this a position is treated as uncovered rather than walking the whole
# contig for every read.
_MAX_FEATURE_SPAN = 3_000_000


def classify_position(index: dict, contig: str, position: int) -> str:
    """Exonic, intronic, or intergenic for one alignment position.

    Exonic wins when a position is both -- overlapping genes on opposite
    strands are common, and mutually exclusive categories are what let the
    three counts sum to the classified total.
    """
    if _covers(index["exons"].get(contig, []), position):
        return "exonic"
    if _covers(index["genes"].get(contig, []), position):
        return "intronic"
    return "intergenic"


class FeatureCounts:
    """Reads by genomic feature class.

    For mRNA the exonic share should dominate; a high intronic share suggests
    immature pre-mRNA, and a high intergenic share suggests genomic DNA
    contamination.
    """

    def __init__(self):
        self._counts = {"exonic": 0, "intronic": 0, "intergenic": 0}

    def add(self, category: str) -> None:
        self._counts[category] += 1

    @property
    def total(self) -> int:
        return sum(self._counts.values())

    def to_facts(self) -> dict:
        return dict(self._counts)


def contig_overlap(bam_contigs: set[str], gtf_contigs: set[str]) -> int:
    """How many contig names the BAM and GTF share.

    Zero is the classic silent failure -- a GTF naming contigs `1,2,3`
    against a BAM naming them `chr1,chr2,chr3`. Every read then falls outside
    every gene and the job produces a plausible-looking 100% intergenic
    result with no error anywhere, so the caller refuses instead.
    """
    return len(bam_contigs & gtf_contigs)
```

- [ ] **Step 4: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_transcript_qc_runner.py -q
```

Expected: PASS, 22 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/transcript_qc_runner.py backend/tests/pipelines/test_transcript_qc_runner.py
git commit -m "feat(pipelines): accumulate gene body coverage and feature distribution"
```

---

### Task 3: The applicability chain

**Files:**
- Create: `backend/app/services/transcript_qc_gating.py`
- Test: `backend/tests/services/test_transcript_qc_gating.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/services/test_transcript_qc_gating.py`:

```python
"""Which BAMs the RNA-seq QC charts apply to.

Inference, not a stored fact -- see the module docstring. The precedence
between signals is the part worth testing: the authoritative field is
unpopulated on real data, so the fallbacks carry the feature.
"""

from app.services.transcript_qc_gating import Applicability, applicability


def _obj(metadata=None, facts=None):
    return {"metadata": metadata or {}, "facts": facts or {}}


class TestApplicability:
    def test_explicit_rna_molecule_type_applies(self):
        got = applicability(_obj(metadata={"molecule_type": "RNA"}))
        assert got.gene_body is True
        assert got.feature_distribution is True
        assert got.reason == "molecule_type"

    def test_explicit_dna_molecule_type_beats_a_splice_aware_aligner(self):
        """An explicit answer outranks every inference below it. STAR is
        routinely used for DNA in some workflows, so the aligner must not
        override a stated molecule type."""
        got = applicability(
            _obj(metadata={"molecule_type": "DNA"}, facts={"aligned_by": "star"})
        )
        assert got.gene_body is False
        assert got.feature_distribution is False

    def test_rnaseq_assay_applies_when_molecule_type_is_missing(self):
        """This is the branch that carries the feature in practice:
        molecule_type is populated on 0 of 9 BAMs in the real database, while
        assay is populated on all 9."""
        got = applicability(_obj(metadata={"assay": "RNA-seq"}))
        assert got.gene_body is True
        assert got.feature_distribution is True
        assert got.reason == "assay"

    def test_chipseq_gets_feature_distribution_only(self):
        """ChIP-seq is DNA, so a gene body curve is meaningless for it -- but
        where its reads fall relative to genes is exactly the question."""
        got = applicability(_obj(metadata={"assay": "ChIP-seq"}))
        assert got.gene_body is False
        assert got.feature_distribution is True

    def test_splice_aware_aligner_applies_as_a_last_resort(self):
        for aligner in ("star", "hisat2"):
            got = applicability(_obj(facts={"aligned_by": aligner}))
            assert got.gene_body is True, aligner
            assert got.reason == "aligner"

    def test_a_dna_aligner_does_not_apply(self):
        got = applicability(_obj(facts={"aligned_by": "bwa-mem2"}))
        assert got.gene_body is False
        assert got.feature_distribution is False

    def test_nothing_known_does_not_apply(self):
        got = applicability(_obj())
        assert got.gene_body is False
        assert got.feature_distribution is False
        assert got.reason is None

    def test_wgs_assay_does_not_apply(self):
        got = applicability(_obj(metadata={"assay": "WGS"}))
        assert got.gene_body is False
        assert got.feature_distribution is False
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/services/test_transcript_qc_gating.py -q
```

Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

Create `backend/app/services/transcript_qc_gating.py`:

```python
"""Whether the RNA-seq QC charts apply to a given BAM.

There is no hard stored answer. `pipeline_service` records that RNA-ness is
not knowable from a BAM's bytes, and while `molecule_type` now exists as a
metadata field, it is only populated when an SRA record supplied it -- zero
of the nine BAMs in a real working database carry it, while `assay` is
populated on all nine and discriminates correctly. Gating on `molecule_type`
alone would therefore ship a feature nobody could see, with a green test
suite.

So this is a fallback chain, strongest signal first, and the result carries
the reason so the UI can say what it inferred from rather than presenting a
guess as a fact.
"""

from dataclasses import dataclass

SPLICE_AWARE_ALIGNERS = {"star", "hisat2"}

# ChIP-seq is DNA, so a gene body curve says nothing -- but where its reads
# sit relative to gene structure is precisely the question being asked.
FEATURE_ONLY_ASSAYS = {"ChIP-seq", "ATAC-seq"}
RNA_ASSAYS = {"RNA-seq"}


@dataclass
class Applicability:
    gene_body: bool
    feature_distribution: bool
    #: Which signal decided it: "molecule_type", "assay", "aligner", or None.
    reason: str | None


_NONE = Applicability(gene_body=False, feature_distribution=False, reason=None)


def applicability(obj: dict) -> Applicability:
    """Decide from metadata and facts, strongest signal first."""
    metadata = obj.get("metadata") or {}
    facts = obj.get("facts") or {}

    molecule_type = metadata.get("molecule_type")
    if molecule_type == "RNA":
        return Applicability(True, True, "molecule_type")
    if molecule_type in {"DNA", "Other"}:
        # An explicit answer outranks every inference below. STAR is used for
        # DNA in some workflows, so the aligner must not override this.
        return _NONE

    assay = metadata.get("assay")
    if assay in RNA_ASSAYS:
        return Applicability(True, True, "assay")
    if assay in FEATURE_ONLY_ASSAYS:
        return Applicability(False, True, "assay")
    if assay:
        # A stated non-RNA assay (WGS, WES, ...) is an answer too.
        return _NONE

    if str(facts.get("aligned_by") or "").lower() in SPLICE_AWARE_ALIGNERS:
        # Weakest signal: a splice-aware aligner is evidence, not proof.
        return Applicability(True, True, "aligner")

    return _NONE
```

- [ ] **Step 4: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/services/test_transcript_qc_gating.py -q
```

Expected: PASS, 8 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/transcript_qc_gating.py backend/tests/services/test_transcript_qc_gating.py
git commit -m "feat(services): infer whether RNA-seq QC applies to a BAM"
```

---

### Task 4: The job handler

**Files:**
- Create: `backend/app/queue/transcript_qc_handlers.py`
- Modify: `backend/app/queue/results.py`

Like `run_bam_stats`, this handler has no unit test — it shells out to pysam and the queue. Its logic lives in the pure functions already covered by Tasks 1-3; verification is Task 7.

- [ ] **Step 1: Write the handler**

Create `backend/app/queue/transcript_qc_handlers.py`:

```python
"""RNA-seq transcript QC: gene body coverage and genomic feature distribution.

One job for both charts. They need the same two expensive things -- a parsed
transcript model and a pass over the BAM -- so computing them separately
would parse the GTF twice and traverse the BAM twice for two charts that sit
side by side.
"""

from pathlib import Path

from app.errors import PermanentError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.pipelines import transcript_qc_runner
from app.queue.registry import HandlerMode, JobContext, handler
from app.storage.sequence_stats import DEFAULT_SAMPLE_READS

log = get_logger(__name__)


@handler(
    "run_transcript_qc",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=1, mem_mb=2048, io=IoClass.HEAVY),
    max_attempts=2,
)
def run_transcript_qc(ctx: JobContext) -> dict:
    """Gene body coverage and feature distribution for one RNA-seq BAM.

    Read-only: derives no files, just facts merged onto the object.
    """
    import pysam

    object_id = ctx.payload.get("object_id")
    if not object_id:
        raise PermanentError("run_transcript_qc requires an 'object_id'")

    bam_path = Path(ctx.payload["bam_path"])
    gtf_path = Path(ctx.payload["gtf_path"])

    ctx.progress(phase="gtf", pct=0.1, message="reading the gene annotation")
    with open(gtf_path, errors="replace") as fh:
        transcripts = transcript_qc_runner.parse_gtf_transcripts(fh)
    representatives = transcript_qc_runner.representative_transcripts(transcripts)
    if not representatives:
        raise PermanentError(
            "No usable transcripts in the annotation. Check that it is a GTF "
            "with 'exon' features and gene_id/transcript_id attributes."
        )
    index = transcript_qc_runner.build_feature_index(representatives)

    gene_body = transcript_qc_runner.GeneBodyCoverage()
    features = transcript_qc_runner.FeatureCounts()
    # Transcripts are looked up per contig by position; a dict keyed by
    # contig keeps the gene-body walk from scanning every gene per read.
    by_contig: dict[str, list] = {}
    for t in representatives:
        by_contig.setdefault(t.contig, []).append(t)
    for v in by_contig.values():
        v.sort(key=lambda t: t.span)

    ctx.progress(phase="reads", pct=0.3, message="classifying reads")
    reads = 0
    with pysam.AlignmentFile(str(bam_path), "rb") as af:
        bam_contigs = set(af.references)
        gtf_contigs = {t.contig for t in representatives}
        if transcript_qc_runner.contig_overlap(bam_contigs, gtf_contigs) == 0:
            # Refuse rather than store a plausible-looking 100% intergenic
            # result -- the '1' vs 'chr1' mismatch produces exactly that.
            raise PermanentError(
                "The annotation and the BAM name their contigs differently "
                f"(BAM: {sorted(bam_contigs)[:3]}..., "
                f"annotation: {sorted(gtf_contigs)[:3]}...). "
                "Use an annotation built against the same reference."
            )

        for contig, per_contig_budget in _sampling_plan(af, DEFAULT_SAMPLE_READS):
            taken = 0
            for rec in af.fetch(contig):
                if rec.is_secondary or rec.is_supplementary or rec.is_unmapped:
                    continue
                if rec.is_duplicate:
                    continue
                position = rec.reference_start + 1
                features.add(
                    transcript_qc_runner.classify_position(index, contig, position)
                )
                for t in by_contig.get(contig, ()):
                    start, end = t.span
                    if start <= position <= end:
                        gene_body.add_read(t, position)
                        break
                reads += 1
                taken += 1
                if taken >= per_contig_budget:
                    break

    if reads == 0:
        raise PermanentError("No usable alignments found in this BAM.")

    facts = {
        "transcript_qc_status": "ok",
        "transcript_qc_sampled_reads": reads,
        "transcript_qc_annotation": ctx.payload.get("gtf_name"),
        "gene_body_coverage": gene_body.to_facts(),
        "feature_distribution": features.to_facts(),
    }

    ctx.progress(phase="done", pct=1.0, message="transcript QC complete")
    log.info(
        "transcript_qc_finished",
        job_id=ctx.job_id,
        object_id=object_id,
        reads=reads,
        exonic=facts["feature_distribution"]["exonic"],
    )

    return {
        "object_id": object_id,
        "project_id": ctx.payload.get("project_id"),
        "job_id": ctx.job_id,
        "facts": facts,
    }


def _sampling_plan(af, budget: int) -> list[tuple[str, int]]:
    """How many reads to take from each contig, proportional to its length.

    Reading the first `budget` records instead -- which is what the existing
    alignment stats do -- takes every read from the start of the first contig
    on a coordinate-sorted BAM. For a gene body curve that is a few hundred
    genes on one chromosome, not a genome-wide answer. See issue #191.
    """
    lengths = [(c, af.get_reference_length(c) or 0) for c in af.references]
    total = sum(n for _, n in lengths)
    if total <= 0:
        return [(c, budget) for c, _ in lengths[:1]]
    plan = []
    for contig, length in lengths:
        share = int(budget * length / total)
        if share > 0:
            plan.append((contig, share))
    return plan or [(lengths[0][0], budget)]
```

- [ ] **Step 2: Register the fact applier**

In `backend/app/queue/results.py`, add next to `_apply_run_bam_stats` (around line 1487):

```python
async def _apply_run_transcript_qc(result: dict, *, owner: str) -> None:
    """Record RNA-seq transcript QC on the BAM it described.

    Read-only like BAM stats: no files to ingest, just facts merged onto the
    object.
    """
    object_id = result.get("object_id")
    facts = result.get("facts") or {}
    if not object_id or not facts:
        return

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        log.warning("transcript_qc_object_missing", object_id=object_id)
        return

    await obj.set(
        {
            DataObject.facts: {**obj.facts, **facts},
            DataObject.updated_at: datetime.now(UTC),
        }
    )

    log.info("transcript_qc_applied", object_id=object_id)
```

And register it in the dispatch dict near line 2428, beside `"run_bam_stats": _apply_run_bam_stats,`:

```python
    "run_transcript_qc": _apply_run_transcript_qc,
```

This dict is exactly the hand-maintained-registry shape CLAUDE.md warns about — a handler with no entry here runs, succeeds, and stores nothing.

- [ ] **Step 3: Register the handler module for import**

**This step is load-bearing and its failure is silent.** `registry.load_handlers()` imports only `app/queue/handlers.py`, which in turn imports every pipeline handler module by name in one explicit list at the bottom of the file (around line 907). A module missing from that list is never imported, so its `@handler` decorator never runs, and the job type simply does not exist — the enqueue fails at runtime with nothing at import time saying why.

Add `transcript_qc_handlers` to that list, in alphabetical position between `summary_handlers` and `tool_handlers`:

```python
from app.queue import (  # noqa: E402, F401
    align_handlers,
    ...
    summary_handlers,
    tool_handlers,
    transcript_qc_handlers,
    uniprot_handlers,
    ...
)
```

- [ ] **Step 3b: Verify the handler actually registered**

Do not take the import on faith — check the registry sees it:

```bash
./backend/run-worktree-tests.sh tests/queue -q
```

Then confirm directly:

```bash
docker compose -p biopipe exec -T api python -c "
from app.queue import registry
registry.load_handlers()
names = sorted(registry.all_handlers())
print('run_transcript_qc' in names)
"
```

Expected: `True`. If the registry exposes its handlers under a different accessor, `grep -n 'def all_handlers\|_HANDLERS' backend/app/queue/registry.py` will name it.

- [ ] **Step 4: Commit**

```bash
git add backend/app/queue/transcript_qc_handlers.py backend/app/queue/results.py
git commit -m "feat(queue): compute RNA-seq gene body coverage and feature distribution"
```

---

### Task 5: Launch path — service and route

**Files:**
- Modify: `backend/app/services/pipeline_service.py`
- Modify: `backend/app/api/v1/pipelines.py`

- [ ] **Step 1: Add the service function**

In `backend/app/services/pipeline_service.py`, after `launch_bam_stats`:

```python
async def launch_transcript_qc(
    *, object_id: PydanticObjectId, gtf_object_id: PydanticObjectId, owner: str
):
    """Queue RNA-seq transcript QC for a BAM against a chosen annotation.

    On demand rather than automatic: applicability is inferred (see
    services/transcript_qc_gating), and an automatic job on a mislabelled DNA
    BAM would burn a full pass to render a meaningless curve. The GTF is
    chosen by the caller rather than guessed, for the same reason.
    """
    from app.queue import queue
    from app.services import object_service

    bam = await object_service.get_object(object_id, owner=owner)
    _check_bam_stats_callable(bam)

    gtf = await object_service.get_object(gtf_object_id, owner=owner)
    if gtf.project_id != bam.project_id:
        raise ValidationError("The annotation must be in the same project as the BAM.")

    _, bam_path = await _resolve_readable(bam)
    _, gtf_path = await _resolve_readable(gtf)
    if not bam_path or not gtf_path:
        raise ValidationError("The BAM and its annotation must both have stored content.")

    job = await queue.enqueue(
        "run_transcript_qc",
        owner=owner,
        payload={
            "object_id": str(bam.id),
            "project_id": str(bam.project_id),
            "bam_path": bam_path,
            "gtf_path": gtf_path,
            "gtf_name": gtf.name,
        },
        job_class=JobClass.COMPUTE,
        resources=JobResources(cpu=1, mem_mb=2048, io=IoClass.HEAVY),
        max_attempts=2,
        # Keyed by both, so re-running against a different annotation is a
        # different job rather than a silently deduped no-op.
        dedup_key=f"transcriptqc:{bam.id}:{gtf.id}",
        project_id=bam.project_id,
        object_id=bam.id,
    )
    if job is None:
        raise ConflictError(
            "Transcript QC is already queued or running for this file",
            details={"object_id": str(bam.id)},
        )
    return job
```

- [ ] **Step 2: Add the route**

In `backend/app/api/v1/pipelines.py`, after the `bamstats` route:

```python
class TranscriptQcRequest(BaseModel):
    object_id: PydanticObjectId
    gtf_object_id: PydanticObjectId


@router.post(
    "/transcript-qc", response_model=JobOut, status_code=status.HTTP_201_CREATED
)
async def launch_transcript_qc(body: TranscriptQcRequest, owner: OwnerDep) -> JobOut:
    """Queue RNA-seq transcript QC: gene body coverage and feature
    distribution. Read-only: produces facts only."""
    job = await pipeline_service.launch_transcript_qc(
        object_id=body.object_id, gtf_object_id=body.gtf_object_id, owner=owner
    )
    return JobOut.of(job)
```

- [ ] **Step 3: Run the suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: PASS. Read the printed count.

- [ ] **Step 4: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/app/api/v1/pipelines.py
git commit -m "feat(api): launch RNA-seq transcript QC against a chosen annotation"
```

---

### Task 6: Frontend

**Files:**
- Modify: `frontend/src/api/types.ts`, `frontend/src/api/client.ts`
- Create: `frontend/src/components/TranscriptQc.tsx`
- Modify: `frontend/src/components/BamResults.tsx`

- [ ] **Step 1: Add types**

In `frontend/src/api/types.ts`, after `DepthHistogramBucket` (or after `InsertSizeHistogramBucket` if plan 1 has not landed):

```typescript
export interface GeneBodyPoint {
  /** 0 = 5' end, 99 = 3' end. */
  percentile: number;
  /** Normalized to the curve's own maximum. */
  coverage: number;
}

export interface FeatureDistribution {
  exonic: number;
  intronic: number;
  intergenic: number;
}
```

And to `BamStatsFacts`:

```typescript
  transcript_qc_status?: "ok";
  transcript_qc_sampled_reads?: number;
  transcript_qc_annotation?: string;
  gene_body_coverage?: GeneBodyPoint[];
  feature_distribution?: FeatureDistribution;
```

- [ ] **Step 2: Add the client call**

In `frontend/src/api/client.ts`, next to `launchBamStats`:

```typescript
  launchTranscriptQc: (objectId: string, gtfObjectId: string) =>
    request<JobSummary>("/pipelines/transcript-qc", {
      method: "POST",
      body: JSON.stringify({ object_id: objectId, gtf_object_id: gtfObjectId }),
    }),
```

- [ ] **Step 3: Write `TranscriptQc.tsx`**

```tsx
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import { notify } from "../stores/messageStore";
import type {
  FeatureDistribution,
  GeneBodyPoint,
  ObjectDetail as ObjectDetailData,
} from "../api/types";

/**
 * RNA-seq QC: where reads sit within a transcript, and where they sit
 * relative to gene structure.
 *
 * On demand rather than automatic, because applicability is inferred rather
 * than known -- there is no stored RNA-vs-DNA flag on a BAM. The button turns
 * a soft signal into a suggestion the user confirms.
 */
export function TranscriptQc({
  obj,
  gtfs,
  geneBody,
  featureDistribution,
}: {
  obj: ObjectDetailData;
  /** GTF objects available in this project. */
  gtfs: { id: string; name: string }[];
  geneBody: boolean;
  featureDistribution: boolean;
}) {
  const qc = useQueryClient();
  const f = obj.facts as {
    transcript_qc_status?: "ok";
    transcript_qc_sampled_reads?: number;
    transcript_qc_annotation?: string;
    gene_body_coverage?: GeneBodyPoint[];
    feature_distribution?: FeatureDistribution;
  };
  const [gtfId, setGtfId] = useState(gtfs[0]?.id ?? "");

  const compute = useMutation({
    mutationFn: () => api.launchTranscriptQc(obj.id, gtfId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      notify.info("Computing transcript QC");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const hasResults = f.transcript_qc_status === "ok";

  if (!hasResults) {
    return (
      <div className="section">
        <div className="section-title">RNA-seq transcript QC</div>
        {gtfs.length === 0 ? (
          // The state every project starts in -- say what to add, rather
          // than leaving a disabled button with no explanation.
          <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
            These charts need a gene annotation (GTF) in this project. Add one
            from NCBI or import your own, then come back.
          </div>
        ) : (
          <>
            <div style={{ color: "var(--text-faint)", fontSize: 12, marginBottom: 8 }}>
              Where reads fall within transcripts (5'→3' bias) and across
              exons, introns, and intergenic space.
            </div>
            {gtfs.length > 1 && (
              <select
                value={gtfId}
                onChange={(e) => setGtfId(e.target.value)}
                style={{ marginRight: 8 }}
              >
                {gtfs.map((g) => (
                  <option key={g.id} value={g.id}>
                    {g.name}
                  </option>
                ))}
              </select>
            )}
            <button
              type="button"
              className="btn"
              onClick={() => compute.mutate()}
              disabled={compute.isPending || !gtfId}
            >
              {compute.isPending ? "Computing…" : "Compute transcript QC"}
            </button>
          </>
        )}
      </div>
    );
  }

  return (
    <div style={{ display: "flex", gap: 24, flexWrap: "wrap" }}>
      {geneBody && f.gene_body_coverage && f.gene_body_coverage.length > 0 && (
        <div className="section" style={{ flex: "1 1 300px" }}>
          <div className="section-title">Gene body coverage</div>
          <GeneBodyChart curve={f.gene_body_coverage} />
          <Provenance
            annotation={f.transcript_qc_annotation}
            reads={f.transcript_qc_sampled_reads}
          />
        </div>
      )}
      {featureDistribution && f.feature_distribution && (
        <div className="section" style={{ flex: "1 1 300px" }}>
          <div className="section-title">Read distribution</div>
          <FeatureBar counts={f.feature_distribution} />
          <Provenance
            annotation={f.transcript_qc_annotation}
            reads={f.transcript_qc_sampled_reads}
          />
        </div>
      )}
    </div>
  );
}

function Provenance({ annotation, reads }: { annotation?: string; reads?: number }) {
  return (
    <div style={{ fontSize: 10, color: "var(--text-faint)", marginTop: 4 }}>
      {annotation ? `${annotation} · ` : ""}
      {reads != null ? `${reads.toLocaleString()} reads sampled` : ""}
    </div>
  );
}

/**
 * Coverage from the 5' end to the 3' end, averaged over genes.
 *
 * A curve that climbs steeply toward the 3' end means the RNA was degraded
 * before sequencing: poly-A selection captures only the surviving 3' tail.
 */
function GeneBodyChart({ curve }: { curve: GeneBodyPoint[] }) {
  const w = 340;
  const h = 150;
  const pad = { top: 10, right: 10, bottom: 22, left: 30 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;

  const x = (p: number) => pad.left + (p / 99) * plotW;
  const y = (v: number) => pad.top + plotH - v * plotH;
  const line = curve
    .map((p, i) => `${i ? "L" : "M"} ${x(p.percentile)} ${y(p.coverage)}`)
    .join(" ");

  return (
    <svg width="100%" viewBox={`0 0 ${w} ${h}`} style={{ maxWidth: w, display: "block" }}>
      {[0, 0.5, 1].map((v) => (
        <g key={v}>
          <line
            x1={pad.left}
            x2={w - pad.right}
            y1={y(v)}
            y2={y(v)}
            stroke="var(--border)"
            strokeWidth="1"
          />
          <text x={pad.left - 4} y={y(v) + 3} textAnchor="end" fontSize="9" fill="var(--text-faint)">
            {v}
          </text>
        </g>
      ))}
      <path d={line} fill="none" stroke="var(--accent)" strokeWidth="1.8" />
      <text x={pad.left} y={h - 6} fontSize="9" fill="var(--text-faint)">
        5′
      </text>
      <text x={w - pad.right} y={h - 6} fontSize="9" fill="var(--text-faint)" textAnchor="end">
        3′
      </text>
    </svg>
  );
}

/**
 * Exonic / intronic / intergenic as one stacked bar.
 *
 * A stacked bar rather than a pie: three categories at very uneven
 * proportions are easier to read and to compare between samples this way,
 * and it matches the app's existing visual language.
 */
function FeatureBar({ counts }: { counts: FeatureDistribution }) {
  const total = counts.exonic + counts.intronic + counts.intergenic;
  if (total === 0) return null;

  const segments = [
    { label: "Exonic", value: counts.exonic, opacity: 1 },
    { label: "Intronic", value: counts.intronic, opacity: 0.66 },
    { label: "Intergenic", value: counts.intergenic, opacity: 0.33 },
  ];

  const w = 340;
  const barH = 26;
  let offset = 0;

  return (
    <div>
      <svg width="100%" viewBox={`0 0 ${w} ${barH}`} style={{ maxWidth: w, display: "block" }}>
        {segments.map((s) => {
          const width = (s.value / total) * w;
          const x = offset;
          offset += width;
          return (
            <rect key={s.label} x={x} y={0} width={width} height={barH} fill="var(--accent)" opacity={s.opacity}>
              <title>
                {s.label}: {s.value.toLocaleString()} (
                {((100 * s.value) / total).toFixed(1)}%)
              </title>
            </rect>
          );
        })}
      </svg>
      <div style={{ display: "flex", gap: 14, flexWrap: "wrap", marginTop: 6, fontSize: 11 }}>
        {segments.map((s) => (
          <div key={s.label} style={{ display: "flex", alignItems: "center", gap: 4 }}>
            <span
              style={{
                width: 9,
                height: 9,
                background: "var(--accent)",
                opacity: s.opacity,
                display: "inline-block",
              }}
            />
            <span style={{ color: "var(--text-faint)" }}>{s.label}</span>
            <span style={{ fontWeight: 600 }}>
              {((100 * s.value) / total).toFixed(1)}%
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Render it from `BamResults.tsx`**

Add the import and mirror the backend chain (the two must agree; a divergence shows as a button that launches a job whose results never render):

```tsx
import { TranscriptQc } from "./TranscriptQc";
```

Add above the Provenance section inside the `hasResults` block:

```tsx
          <TranscriptQc
            obj={obj}
            gtfs={gtfObjects}
            geneBody={rnaApplicability.geneBody}
            featureDistribution={rnaApplicability.featureDistribution}
          />
```

with this helper at the bottom of the file:

```tsx
/** Mirrors backend services/transcript_qc_gating.py -- keep the two in step. */
function transcriptQcApplicability(obj: ObjectDetailData) {
  const md = (obj.metadata ?? {}) as Record<string, unknown>;
  const molecule = md.molecule_type;
  if (molecule === "RNA") return { geneBody: true, featureDistribution: true };
  if (molecule === "DNA" || molecule === "Other")
    return { geneBody: false, featureDistribution: false };

  const assay = md.assay;
  if (assay === "RNA-seq") return { geneBody: true, featureDistribution: true };
  if (assay === "ChIP-seq" || assay === "ATAC-seq")
    return { geneBody: false, featureDistribution: true };
  if (assay) return { geneBody: false, featureDistribution: false };

  const aligner = String(obj.facts.aligned_by ?? "").toLowerCase();
  if (aligner === "star" || aligner === "hisat2")
    return { geneBody: true, featureDistribution: true };

  return { geneBody: false, featureDistribution: false };
}
```

In the component body, compute it and gate the whole block:

```tsx
  const rnaApplicability = transcriptQcApplicability(obj);
  const rnaApplies =
    rnaApplicability.geneBody || rnaApplicability.featureDistribution;
```

and wrap the `<TranscriptQc .../>` above in `{rnaApplies && ( ... )}`.

`gtfObjects` comes from the project's object list. If `BamResults` has no such list in scope, pass it down from the parent that already queries project objects rather than adding a second query here — check `DetailPanel.tsx` for what is already available.

- [ ] **Step 5: Typecheck**

```bash
cd frontend && npm run lint
```

Expected: no output, exit 0.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/api/client.ts frontend/src/components/TranscriptQc.tsx frontend/src/components/BamResults.tsx
git commit -m "feat(frontend): plot gene body coverage and read distribution"
```

---

### Task 7: Verify against real objects

Every fixture in Tasks 1-3 feeds hand-built objects that already look the way the code expects — the exact shape of green-suite-wrong-behaviour this repo has hit before. This task is not optional.

- [ ] **Step 1: Bring up the worktree stack**

```bash
./ops/worktree-up.sh
```

- [ ] **Step 2: Import a GTF**

There are **zero GTF objects** in the seeded database, so this walks the empty state first. Open a project with the STAR-aligned RNA-seq BAMs (`ERR458494.bam` and siblings) at http://localhost:5273. Confirm the transcript QC section shows the "needs a gene annotation" message and no button.

Then download an annotation for the matching reference via the NCBI dialog, or import one. The `ERR458*` samples are *S. cerevisiae*, so a yeast GTF is small and quick.

- [ ] **Step 3: Run it**

Press **Compute transcript QC**. When it finishes, check:

- The gene body curve renders, 5' on the left, 3' on the right.
- The read distribution bar renders with three segments summing to 100%.
- **The exonic share dominates.** For RNA-seq it should be the large majority. A near-100% *intergenic* result means the contig-name check did not catch a mismatch, or classification is broken — investigate rather than accepting the number.

- [ ] **Step 4: Confirm the contig-mismatch guard actually fires**

The check that matters most, because its failure mode is a plausible number rather than an error. Point the job at an annotation for a *different* organism:

```bash
docker compose -p biopipe-issue-129-37ae8a exec -T api python -c "
import asyncio
from app.db.client import connect_to_mongo, get_db
async def main():
    await connect_to_mongo()
    db = get_db()
    async for o in db.objects.find({'facts.transcript_qc_status':'ok'}, {'name':1,'facts':1}).limit(3):
        f=o['facts']
        fd=f['feature_distribution']
        total=sum(fd.values())
        print(o['name'], '| reads:', f['transcript_qc_sampled_reads'], '| annotation:', f.get('transcript_qc_annotation'))
        print('   exonic %.1f%% intronic %.1f%% intergenic %.1f%%' % tuple(100*fd[k]/total for k in ('exonic','intronic','intergenic')))
        gb=f['gene_body_coverage']
        print('   gene body points:', len(gb), '(expect 100), peak at percentile', max(gb, key=lambda p: p['coverage'])['percentile'])
asyncio.run(main())
"
```

Expected: 100 gene-body points; exonic dominant; a peak percentile that is not pinned at 0 or 99 unless the sample is genuinely degraded.

- [ ] **Step 5: Check a DNA BAM does not offer the feature**

Open `ERR17609896.bam` (assay `WGS`). The transcript QC section must not appear at all. This is the gating direction that fails when the chain breaks — the other direction passes whether or not the chain works.

- [ ] **Step 6: Tear down and run the full suite**

```bash
./ops/worktree-up.sh --down
```

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: PASS. Read the count.

---

### Task 8: Open the PR

- [ ] **Step 1: Push**

```bash
git push -u origin HEAD
```

- [ ] **Step 2: Open the PR**

```bash
gh pr create --base main --title "feat(pipelines): add RNA-seq gene body coverage and read distribution" --body "$(cat <<'EOF'
Two RNA-seq alignment QC charts, computed in one job: gene body coverage
(5'->3' bias, the RNA-degradation signature) and the exonic / intronic /
intergenic read distribution.

One job because both need the same two expensive things -- a parsed transcript
model from a GTF and a pass over the BAM -- so computing them separately would
parse the GTF twice and traverse the BAM twice for two charts that sit side by
side.

Custom pysam rather than RSeQC: it avoids a new system dependency plus its
TOOL_META/licence/suggestion wiring for what is a few dozen lines, and it
matches how the comparable insert-size and MAPQ histograms were actually built
here. The numbers will not match RSeQC to the decimal; these charts are read
for shape, and the binning choices are fixed in the spec so the shape is
stable.

The gating is the part worth reviewing. There is no stored RNA-vs-DNA flag,
and while `molecule_type` exists as a metadata field it is populated on 0 of 9
BAMs in a real database (it only lands with an SRA record), while `assay` is
populated on all 9 and discriminates correctly. Gating on `molecule_type`
alone would have shipped a feature nobody could see, with a green suite. So
applicability is a fallback chain -- explicit molecule_type, then assay, then
aligner -- and the job is on demand behind a button rather than automatic,
since inference can be wrong.

ChIP-seq gets the read-distribution chart only: it is DNA, so a gene body
curve is meaningless for it, which is why the job writes two independent facts
rather than one blob.

A contig-name mismatch ('1' vs 'chr1') is refused up front. It is the classic
silent failure here -- every read falls outside every gene and the job would
otherwise store a plausible-looking 100% intergenic result with no error.

Reads are sampled strided across contigs rather than from the head of the
file, so the curve is genome-wide (see #191 for the same bias in the existing
alignment stats).

Closes #158
Closes #159
EOF
)"
```

- [ ] **Step 3: Label the PR**

```bash
gh pr edit --add-label "type:feature" --add-label "area:pipelines" --add-label "area:frontend" --add-label "area:backend"
```

- [ ] **Step 4: Report the PR URL and stop.** Do not merge.

---

## Self-review

**Spec coverage**

| Spec requirement | Task |
|---|---|
| One job, one GTF parse, one BAM pass | 4 |
| Custom pysam, no new tool | 4 |
| Longest transcript per gene | 1 |
| ~200 bp transcript length floor | 1 |
| 100 bins, strand-corrected | 2 |
| Normalized to max | 2 |
| Exonic wins ties; categories exclusive | 2 |
| Skip secondary/supplementary/duplicate | 4 |
| Contig-overlap check, fail loudly | 2 (pure), 4 (raises), 7 (verified) |
| Strided sampling, count recorded | 4 |
| Applicability chain, 5 branches | 3 |
| ChIP-seq → feature distribution only | 3 |
| On-demand button | 5, 6 |
| GTF selection: none / one / several | 6 |
| Facts `gene_body_coverage`, `feature_distribution` | 4 |
| Hand-rolled SVG; stacked bar not pie | 6 |
| States GTF used and reads sampled | 6 |
| Registry check (`results.py` dispatch) | 4 |
| Real-object verification, unavailable direction | 7 |

**Placeholder scan:** none. Task 6 Step 4 leaves *where* `gtfObjects` is sourced to inspection of `DetailPanel.tsx` rather than inventing a prop chain — flagged explicitly as a thing to check, not a blank to fill.

**Type consistency:** `Transcript(transcript_id, gene_id, contig, strand, exons)` with `.length` / `.span` is used identically in Tasks 1, 2, 4. `GeneBodyCoverage.add_read(transcript, position)` / `.to_facts()`, `FeatureCounts.add(category)` / `.to_facts()` / `.total`, `build_feature_index` → `classify_position(index, contig, position)`, and `contig_overlap(bam, gtf)` match between definition (Task 2) and use (Task 4). `Applicability(gene_body, feature_distribution, reason)` matches its use in Task 3's tests; the frontend mirror in Task 6 returns camelCase `geneBody` / `featureDistribution`, matching `TranscriptQc`'s props. Fact keys `gene_body_coverage` / `feature_distribution` / `transcript_qc_*` agree across Tasks 4 and 6.

**One risk worth naming:** `_covers` uses a bisect-and-scan-back over sorted intervals with a `_MAX_FEATURE_SPAN` bound rather than a real interval tree. That is correct for the overlap cases tested and cheap to build, but a pathological annotation with many very long overlapping genes would scan more than it should. If Task 7 shows the job running slowly on a real genome, replace `_covers` with an interval tree — the seam is one function and its tests do not change.
