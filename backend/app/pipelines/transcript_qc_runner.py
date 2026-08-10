"""Transcript-model parsing and RNA-seq QC accumulation.

Kept separate from the job handler so the parts worth testing -- GTF parsing,
representative-transcript choice, and both accumulators -- are pure functions
over strings and lists, with no queue, filesystem, or pysam involved. Mirrors
bam_stats_runner.py's split for the same reason.
"""

import bisect
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
    """One transcript's exon structure, in reference coordinates.

    `exons` is kept sorted by coordinate at all times -- enforced in
    __post_init__ rather than only by parse_gtf_transcripts, so `.span`
    (which assumes exons[0] is the lowest-coordinate exon and exons[-1] is
    the highest) can never be silently wrong regardless of construction
    order.
    """

    transcript_id: str
    gene_id: str
    contig: str
    strand: str
    exons: list[tuple[int, int]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.exons.sort()

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
    of raising. Matches the exact attribute name, not a prefix -- GTF
    attribute columns routinely carry keys like `gene_biotype` alongside
    `gene_id`, and prefix matching would silently return the wrong value
    for a query like `gene_id` against a column ordered with `gene_id_2`
    (or similar) first.
    """
    for part in attrs.split(";"):
        part = part.strip()
        if not part:
            continue
        name, _, value = part.partition(" ")
        if name != key:
            continue
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
        if transcript.strand == "-":
            # The 5' end of a minus-strand transcript is its highest
            # coordinate, so the offset measured in ascending coordinates
            # runs 3'->5' and has to be reversed.
            offset = length - 1 - offset
        # Integer arithmetic throughout: offset/length as a float and then
        # multiplying by bins introduces rounding error right at bin
        # boundaries (e.g. 570/1000*100 == 56.99999999999999, not 57),
        # which silently misfiles a read into the neighboring bin and can
        # break the plus/minus symmetry this class depends on. The
        # single-division integer form is exact.
        idx = min((offset * self.bins) // length, self.bins - 1)
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
    necessarily the containing one -- scan back through every interval
    starting at or before `position` and check whether it also ends at or
    after it. This is O(k) in the number of intervals with start <= position,
    not O(1) -- a real interval tree (tracking max-end-in-subtree) would do
    better, but is out of scope here since the interval counts this deals
    with (exons/genes on one contig) are small relative to the BAM pass this
    runs inside.

    Earlier versions of this function stopped the scan early once an
    interval's start fell more than a fixed distance before `position`, on
    the assumption real gene/intron spans stay under that distance. That
    assumption doesn't hold in general -- a malformed annotation or an
    unusually large feature can have a start arbitrarily far before a
    position it still covers -- and the early stop produced a silent false
    "not covered" in exactly that case. There is no correct way to bound the
    scan without a data structure that tracks it explicitly, so this no
    longer tries.
    """
    i = bisect.bisect_right(intervals, (position, float("inf")))
    for start, end in intervals[:i]:
        if end >= position:
            return True
    return False


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
