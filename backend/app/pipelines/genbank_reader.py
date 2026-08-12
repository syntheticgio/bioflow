"""Streaming reader for GenBank flat files.

The piece GFF did not need. A GFF feature is one line, so the handler can
loop over lines directly; a GenBank feature spans a location that may wrap
and a qualifier block that may wrap again, so features have to be grouped
into records before they can be parsed.

What this module guarantees is memory: a record's ORIGIN block is stepped
over line by line and never accumulated. A .gbff whose bulk is sequence
therefore costs no more than one whose bulk is features, which is what keeps
`build_annotation_db`'s streaming design intact.
"""

import gzip
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GenBankRecord:
    """One record: enough header to name a contig, plus its feature lines."""

    accession: str
    length: int | None = None
    source: str = ""
    has_sequence: bool = False
    feature_lines: list[str] = field(default_factory=list)


def _open_text(path: Path):
    """Gzip-aware line reader.

    Sniffed by magic bytes rather than extension, matching
    `annotation_handlers._open_text`: a file downloaded from NCBI is gzipped
    whether or not whoever renamed it kept the suffix.
    """
    with open(path, "rb") as probe:
        magic = probe.read(2)
    if magic == b"\x1f\x8b":
        return gzip.open(path, "rt", errors="replace")
    return open(path, errors="replace")


def iter_records(path: Path):
    """Yield one GenBankRecord at a time.

    Never holds more than one record's feature lines in memory, and never
    holds any sequence at all.
    """
    record: GenBankRecord | None = None
    locus_name = ""
    version = ""
    accession = ""
    in_features = False
    in_origin = False

    def flush():
        """Close the current record, naming its contig.

        VERSION, then ACCESSION, then the LOCUS name. The versioned accession
        is what NCBI's paired FASTA uses in its deflines, so a GenBank and its
        sibling FASTA agree on contig names -- which they must, because contig
        lengths may arrive from a reference's facts and are matched by name.
        """
        nonlocal record
        if record is None:
            return None
        record.accession = version or accession or locus_name or "unknown"
        out = record
        record = None
        return out

    with _open_text(path) as fh:
        for raw in fh:
            line = raw.rstrip("\n")

            if line.startswith("LOCUS"):
                previous = flush()
                if previous is not None:
                    yield previous
                in_features = in_origin = False
                locus_name = version = accession = ""
                record = GenBankRecord(accession="")
                parts = line.split()
                if len(parts) > 1:
                    locus_name = parts[1]
                # `LOCUS name 4641652 bp ...` -- the token before `bp`.
                for i, token in enumerate(parts):
                    if token == "bp" and i > 0 and parts[i - 1].isdigit():
                        record.length = int(parts[i - 1])
                        break
                continue

            if record is None:
                continue

            if line.startswith("//"):
                out = flush()
                if out is not None:
                    yield out
                in_features = in_origin = False
                continue

            if line.startswith("ORIGIN"):
                in_features = False
                in_origin = True
                continue

            if in_origin:
                # Stepped over, never stored. Any non-blank content here is
                # sequence, which is the whole point of this branch.
                if line.strip():
                    record.has_sequence = True
                continue

            if line.startswith("VERSION"):
                parts = line.split()
                if len(parts) > 1:
                    version = parts[1]
                continue

            if line.startswith("ACCESSION"):
                parts = line.split()
                if len(parts) > 1:
                    accession = parts[1]
                continue

            if line.startswith("SOURCE"):
                record.source = line[len("SOURCE"):].strip()
                continue

            if line.startswith("FEATURES"):
                in_features = True
                continue

            # A keyword in column 1 ends the feature block.
            if line[:1].strip():
                in_features = False
                continue

            if in_features:
                record.feature_lines.append(line)

    out = flush()
    if out is not None:
        yield out
