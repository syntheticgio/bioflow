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
