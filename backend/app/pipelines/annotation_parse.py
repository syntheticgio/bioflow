"""One normalized feature row from a GFF3, GTF, or BED line.

Pure functions with no I/O so the format edge cases -- which is most of what
this file is -- are testable as plain calls.

The normalization that matters is coordinates. BED is zero-based half-open;
GFF and GTF are one-based inclusive. Everything downstream (the locus jump,
the coverage accumulator, the table) assumes one-based inclusive, so BED is
converted here and nowhere else.
"""

from dataclasses import dataclass
from urllib.parse import unquote


@dataclass(frozen=True)
class Feature:
    """One row of the features table.

    `parent` being None is what makes a feature top-level, which is the
    property the table pages over -- see the spec's paging decision.
    """

    contig: str
    start: int
    end: int
    type: str | None
    strand: str | None
    score: float | None
    name: str | None
    feature_id: str | None
    parent: str | None
    biotype: str | None
    attributes: str | None


# Lines that are structural rather than data, in any of the three formats.
_SKIP_PREFIXES = ("#", "track", "browser")


def _score(value: str) -> float | None:
    """A score column, which is '.' when absent in every format here.

    None rather than 0.0, the same reasoning `variant_db._num` documents: an
    absent score is not a score of zero, and storing it as one would sort the
    row to the bottom of a score-ordered view rather than out of it.
    """
    if value in (".", ""):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _strand(value: str) -> str | None:
    return value if value in ("+", "-") else None


def parse_gff_attributes(column: str) -> dict[str, str]:
    """GFF3's 9th column: `key=value;key=value`, percent-encoded.

    Malformed pairs are skipped rather than raising -- a single bad attribute
    must not cost the whole feature, which is the posture `bakta_runner.
    parse_gff3` documents for the same data.
    """
    if not column or column == ".":
        return {}
    out: dict[str, str] = {}
    for pair in column.split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        out[key.strip()] = unquote(value.strip())
    return out


def parse_gtf_attributes(column: str) -> dict[str, str]:
    """GTF's 9th column: `key "value"; key "value";`.

    Unquoted values are accepted too; some writers emit bare numbers for
    `exon_number`.
    """
    if not column or column == ".":
        return {}
    out: dict[str, str] = {}
    for pair in column.split(";"):
        pair = pair.strip()
        if not pair:
            continue
        key, _, value = pair.partition(" ")
        if not value:
            continue
        out[key.strip()] = value.strip().strip('"')
    return out


def _tabular_fields(line: str, minimum: int) -> list[str] | None:
    """Split a data line, or None if it is structural or too short."""
    if not line or line.startswith(_SKIP_PREFIXES):
        return None
    fields = line.rstrip("\n").split("\t")
    if len(fields) < minimum:
        return None
    return fields


def parse_gff_line(line: str) -> Feature | None:
    """One GFF3 data line. None for comments, blanks, and malformed rows."""
    fields = _tabular_fields(line, 9)
    if fields is None:
        return None
    try:
        start = int(fields[3])
        end = int(fields[4])
    except ValueError:
        return None

    attrs = parse_gff_attributes(fields[8])

    # GFF3 allows Parent=a,b for an exon shared by two transcripts. The
    # table's tree is single-parent, so the first wins; the raw attribute
    # column preserves both for the expanded detail row.
    parent = attrs.get("Parent")
    if parent:
        parent = parent.split(",")[0]

    return Feature(
        contig=fields[0],
        start=start,
        end=end,
        type=fields[2] or None,
        strand=_strand(fields[6]),
        score=_score(fields[5]),
        name=attrs.get("Name") or attrs.get("gene") or attrs.get("ID"),
        feature_id=attrs.get("ID"),
        parent=parent or None,
        biotype=attrs.get("gene_biotype") or attrs.get("biotype"),
        attributes=fields[8],
    )


def parse_gtf_line(line: str) -> Feature | None:
    """One GTF data line.

    GTF has no ID/Parent. Hierarchy is inferred from the identifier columns:
    a transcript belongs to its gene_id, and anything below a transcript
    (exon, CDS, UTR) belongs to its transcript_id. A row carrying neither is
    top-level.

    A sub-transcript row missing transcript_id falls back to gene_id rather
    than becoming top-level -- orphaning it would inflate the parent count
    the table pages over.
    """
    fields = _tabular_fields(line, 9)
    if fields is None:
        return None
    try:
        start = int(fields[3])
        end = int(fields[4])
    except ValueError:
        return None

    attrs = parse_gtf_attributes(fields[8])
    ftype = fields[2] or None
    gene_id = attrs.get("gene_id")
    transcript_id = attrs.get("transcript_id")

    if ftype == "gene":
        feature_id, parent = gene_id, None
    elif ftype == "transcript":
        feature_id, parent = transcript_id, gene_id
    else:
        # exon/CDS/UTR rows: GTF gives them no identifier of their own.
        # parent is the transcript when one is named, else the gene
        # directly -- a CDS row missing transcript_id still attaches to
        # something rather than becoming a top-level, parentless row.
        parent = transcript_id or gene_id
        feature_id = transcript_id or gene_id

    return Feature(
        contig=fields[0],
        start=start,
        end=end,
        type=ftype,
        strand=_strand(fields[6]),
        score=_score(fields[5]),
        name=attrs.get("gene_name") or attrs.get("gene_id"),
        feature_id=feature_id,
        parent=parent,
        biotype=attrs.get("gene_biotype") or attrs.get("gene_type"),
        attributes=fields[8],
    )


def parse_bed_line(line: str) -> Feature | None:
    """One BED data line, converted to one-based inclusive coordinates.

    BED's [start, end) with a zero-based start is the same interval as GFF's
    [start+1, end] one-based inclusive. See the module docstring: this is the
    only place the conversion happens.
    """
    fields = _tabular_fields(line, 3)
    if fields is None:
        return None
    try:
        start = int(fields[1])
        end = int(fields[2])
    except ValueError:
        return None

    return Feature(
        contig=fields[0],
        start=start + 1,
        end=end,
        type=None,
        strand=_strand(fields[5]) if len(fields) > 5 else None,
        score=_score(fields[4]) if len(fields) > 4 else None,
        name=fields[3] if len(fields) > 3 and fields[3] != "." else None,
        feature_id=None,
        parent=None,
        biotype=None,
        attributes=None,
    )
