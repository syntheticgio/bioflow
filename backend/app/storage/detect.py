"""Bioinformatics file-format detection from magic bytes and extension.

Both signals are recorded rather than merged into one answer. A file named
`.bam` whose contents are gzipped FASTQ is a real and confusing situation, and
the user is better served by seeing the disagreement than by a silent guess.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.logging import get_logger
from app.models import Compression, FormatConfidence, FormatInfo, FormatKind

log = get_logger(__name__)

HEAD_BYTES = 65536

# --- Magic signatures ---
GZIP_MAGIC = b"\x1f\x8b"
BAM_MAGIC = b"BAM\x01"
CRAM_MAGIC = b"CRAM"
BCF_MAGIC = b"BCF\x02"
BZIP2_MAGIC = b"BZh"
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

EXTENSION_MAP: dict[str, FormatKind] = {
    "fastq": FormatKind.FASTQ,
    "fq": FormatKind.FASTQ,
    "fasta": FormatKind.FASTA,
    "fa": FormatKind.FASTA,
    "fna": FormatKind.FASTA,
    "faa": FormatKind.FASTA,
    "fas": FormatKind.FASTA,
    "bam": FormatKind.BAM,
    "sam": FormatKind.SAM,
    "cram": FormatKind.CRAM,
    "vcf": FormatKind.VCF,
    "bcf": FormatKind.BCF,
    "bed": FormatKind.BED,
    "gff": FormatKind.GFF,
    "gff3": FormatKind.GFF,
    "gtf": FormatKind.GTF,
    "gb": FormatKind.GENBANK,
    "gbk": FormatKind.GENBANK,
    "gbff": FormatKind.GENBANK,
    "genbank": FormatKind.GENBANK,
    "gfa": FormatKind.GFA,
    "fai": FormatKind.FAI,
    "txt": FormatKind.TEXT,
    "tsv": FormatKind.TEXT,
}

COMPRESSION_EXTENSIONS = {"gz", "bgz", "bgzf", "zst", "zstd", "bz2"}


@dataclass
class DetectionResult:
    kind: FormatKind
    compression: Compression
    confidence: FormatConfidence
    extension_says: FormatKind | None
    magic_says: FormatKind | None

    def to_format_info(self) -> FormatInfo:
        return FormatInfo(
            kind=self.kind,
            compression=self.compression,
            confidence=self.confidence,
            extension_says=self.extension_says,
            magic_says=self.magic_says,
            detected_at=datetime.now(UTC),
        )


def detect_from_extension(filename: str) -> FormatKind | None:
    """Read the last meaningful suffix, skipping a compression suffix."""
    parts = filename.lower().rstrip(".").split(".")
    for part in reversed(parts[1:]):
        if part in COMPRESSION_EXTENSIONS:
            continue
        return EXTENSION_MAP.get(part)
    return None


def strip_compression_suffix(filename: str) -> str:
    """Drop a trailing compression suffix (`.gz`, `.bgz`, ...) if present.

    Case-preserving, unlike `detect_from_extension`: this returns a name to
    display or store, not a kind to classify. Used when a name's claimed
    compression suffix disagrees with what the bytes actually are (see
    storage/compress.py), so a fresh `.gz` is not appended on top of a stale,
    incorrect one.
    """
    for ext in sorted(COMPRESSION_EXTENSIONS, key=len, reverse=True):
        suffix = f".{ext}"
        if filename.lower().endswith(suffix):
            return filename[: -len(suffix)]
    return filename


def detect_compression(head: bytes) -> Compression:
    if head.startswith(GZIP_MAGIC):
        # BGZF is gzip with a specific extra-field subfield ("BC", length 2).
        # The distinction is load-bearing: BGZF is block-compressed and thus
        # randomly seekable, which is what makes an index possible.
        return Compression.BGZF if _is_bgzf(head) else Compression.GZIP
    if head.startswith(BZIP2_MAGIC):
        return Compression.BZIP2
    if head.startswith(ZSTD_MAGIC):
        return Compression.ZSTD
    return Compression.NONE


def _is_bgzf(head: bytes) -> bool:
    if len(head) < 18 or not head.startswith(GZIP_MAGIC):
        return False
    flg = head[3]
    if not flg & 0x04:  # FEXTRA must be set
        return False
    xlen = int.from_bytes(head[10:12], "little")
    extra = head[12 : 12 + xlen]
    i = 0
    while i + 4 <= len(extra):
        si1, si2 = extra[i], extra[i + 1]
        slen = int.from_bytes(extra[i + 2 : i + 4], "little")
        if si1 == ord("B") and si2 == ord("C"):
            return True
        i += 4 + slen
    return False


def _decompress_head(head: bytes, compression: Compression) -> bytes:
    """Best-effort decompression of a truncated head for text sniffing.

    `head` is a prefix, so a truncation error is the *expected* outcome, not a
    failure. Note zlib.error is not an OSError subclass -- catching only OSError
    lets a corrupt stream escape and turns an unreadable file into a retrying,
    never-succeeding ingest job.

    BGZF (and any multi-member gzip stream) packs many independent members back
    to back -- a real bgzip'd file's first member is a ~66-byte header block,
    far short of one line of sniffable text. Decoding only the first member (as
    a single `gzip.decompress` or `zlib.decompressobj` call does) yields too
    little for `_sniff_text` to recognize anything, so every member available
    within `head` is decoded and concatenated.
    """
    import zlib

    if compression in (Compression.GZIP, Compression.BGZF):
        out = bytearray()
        data = head
        while data:
            decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
            try:
                out += decompressor.decompress(data)
            except zlib.error:
                break
            if not decompressor.eof:
                # Final member truncated mid-stream -- expected for a prefix.
                break
            remaining = decompressor.unused_data
            if not remaining or remaining == data:
                break
            data = remaining
        return bytes(out)
    if compression is Compression.NONE:
        return head
    return b""  # zstd/bzip2 heads are not sniffed in this phase


def detect_from_magic(head: bytes) -> tuple[FormatKind | None, Compression]:
    compression = detect_compression(head)
    payload = _decompress_head(head, compression)

    # Binary formats live inside the (usually BGZF) container.
    if payload.startswith(BAM_MAGIC):
        return FormatKind.BAM, compression
    if payload.startswith(BCF_MAGIC):
        return FormatKind.BCF, compression
    if payload.startswith(CRAM_MAGIC) or head.startswith(CRAM_MAGIC):
        return FormatKind.CRAM, compression

    kind = _sniff_text(payload)
    return kind, compression


def _sniff_text(payload: bytes) -> FormatKind | None:
    if not payload:
        return None
    try:
        text = payload.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None

    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None

    first = lines[0]

    if first.startswith("##fileformat=VCF"):
        return FormatKind.VCF
    if first.startswith("##gff-version"):
        return FormatKind.GFF
    if first.startswith(("@HD", "@SQ", "@RG", "@PG")):
        return FormatKind.SAM
    if first.startswith(">"):
        return FormatKind.FASTA

    # A GenBank record must open with LOCUS in column 1 -- the format's own
    # spec fixes that, which makes this a real positive signal rather than a
    # shape heuristic like the tabular sniffing below. The trailing
    # whitespace check matters: a prose file starting "LOCUSTS ..." is not a
    # GenBank record.
    if first.startswith("LOCUS") and first[5:6].isspace():
        return FormatKind.GENBANK

    # FASTQ: '@' header, then sequence, '+' separator, then equal-length quality.
    # The '+' at line 3 is what separates it from a '@'-prefixed SAM header.
    if first.startswith("@") and len(lines) >= 4 and lines[2].startswith("+"):
        if len(lines[1]) == len(lines[3]):
            return FormatKind.FASTQ
        return FormatKind.FASTQ

    data_lines = [ln for ln in lines if not ln.startswith("#")]

    # Before the tabular heuristic: a GFA is tab-separated, and while its
    # S-lines happen not to look like BED (columns 2 and 3 are a name and a
    # sequence, not integers), relying on that coincidence would make the
    # answer depend on which record type came first.
    if _looks_like_gfa(data_lines):
        return FormatKind.GFA

    if data_lines:
        return _sniff_tabular(data_lines)
    return None


# The record types GFA 1.0 defines. Segments and links are the two that carry
# the graph; a file of nothing but headers and comments is not evidence.
_GFA_RECORD_TYPES = frozenset("HSLCPWJ")


def _looks_like_gfa(lines: list[str]) -> bool:
    """Every line a GFA record, with the segment and link fields validated.

    Checking only the leading letter is not enough, and this is not
    hypothetical -- `P\\t1\\t2` / `S\\t3\\t4`, an ordinary two-column table,
    passed that version. Unlike BED-vs-GFF, where both answers are at least
    intervals, calling a data table an assembly graph is a category error the
    explorer shows to the user, so the fields get checked:

    - `S` is `name`, then either `*` or an actual nucleotide sequence
    - `L` is `from`, orientation, `to`, orientation, where orientation is +/-

    A file of nothing but headers and comments is still not evidence, so at
    least one validated segment or link is required.
    """
    sample = lines[: min(20, len(lines))]
    if not sample:
        return False
    saw_graph_record = False
    for line in sample:
        cols = line.split("\t")
        if len(cols) < 2 or cols[0] not in _GFA_RECORD_TYPES:
            return False
        if cols[0] == "S":
            if len(cols) < 3 or not _is_sequence_field(cols[2]):
                return False
            saw_graph_record = True
        elif cols[0] == "L":
            if len(cols) < 5 or cols[2] not in ("+", "-") or cols[4] not in ("+", "-"):
                return False
            saw_graph_record = True
    return saw_graph_record


# IUPAC, because an assembler may emit ambiguity codes and a graph full of N
# is still a graph.
_SEQUENCE_RE = re.compile(r"^[ACGTURYKMSWBDHVNacgturykmswbdhvn]+$")


def _is_sequence_field(value: str) -> bool:
    """A GFA segment's sequence: literal bases, or `*` for 'not stored'."""
    return value == "*" or bool(_SEQUENCE_RE.match(value))


def _sniff_tabular(lines: list[str]) -> FormatKind | None:
    """Distinguish BED from GTF/GFF. Neither has magic bytes, so this is weak
    evidence by design -- the extension gets a vote for these formats.

    Column count and "two ints" alone are not enough: a samtools `.fai` index
    (`name, length, offset, linebases, linewidth`) has 5 tab-separated columns
    with both columns 1 and 2 numeric, purely by coincidence (#48). BED's
    columns 1 and 2 are chromStart < chromEnd -- an ordered interval -- while a
    `.fai`'s length and offset have no such relationship, so requiring the
    ordering is a real positive signal rather than only a shape check.
    """
    sample = lines[: min(5, len(lines))]
    for line in sample:
        cols = line.split("\t")
        if len(cols) < 3:
            return None
        # GFF/GTF: 9 columns with numeric coordinates in 4 and 5.
        if len(cols) >= 8 and _is_int(cols[3]) and _is_int(cols[4]):
            return FormatKind.GFF
        # BED: chrom, chromStart, chromEnd, with chromStart < chromEnd.
        if not (_is_int(cols[1]) and _is_int(cols[2]) and int(cols[1]) < int(cols[2])):
            return None
    return FormatKind.BED


def _is_int(s: str) -> bool:
    return bool(re.match(r"^\d+$", s.strip()))


def detect(path: Path, filename: str | None = None) -> DetectionResult:
    """Identify a file from its first 64 KiB plus its name."""
    name = filename or path.name
    ext_kind = detect_from_extension(name)

    try:
        with open(path, "rb") as f:
            head = f.read(HEAD_BYTES)
    except OSError:
        return DetectionResult(
            kind=ext_kind or FormatKind.UNKNOWN,
            compression=Compression.NONE,
            confidence=FormatConfidence.EXTENSION if ext_kind else FormatConfidence.NONE,
            extension_says=ext_kind,
            magic_says=None,
        )

    # Detection must never raise: it runs on arbitrary user bytes, and a
    # malformed file has to end up flagged rather than stuck in a retry loop.
    try:
        magic_kind, compression = detect_from_magic(head)
    except Exception as e:  # noqa: BLE001
        log.warning("magic_detection_failed", path=str(path), error=str(e))
        magic_kind, compression = None, Compression.NONE

    if magic_kind is not None:
        kind, confidence = magic_kind, FormatConfidence.MAGIC
    elif ext_kind is not None:
        kind, confidence = ext_kind, FormatConfidence.EXTENSION
    else:
        kind, confidence = FormatKind.UNKNOWN, FormatConfidence.NONE

    # BED and GTF are sniffed only weakly (no magic), so a matching extension is
    # better evidence than the tabular heuristic.
    if ext_kind in (FormatKind.BED, FormatKind.GTF, FormatKind.GFF) and magic_kind in (
        FormatKind.BED,
        FormatKind.GFF,
    ):
        kind = ext_kind

    return DetectionResult(
        kind=kind,
        compression=compression,
        confidence=confidence,
        extension_says=ext_kind,
        magic_says=magic_kind,
    )
