"""Format-specific header parsing.

Everything here reads *headers and a small prefix*, never the whole file. A
100 GB BAM has its full metadata in the first few kilobytes; scanning the body
to count records would take minutes and tell the user almost nothing extra.

Where an exact answer would require a full scan (FASTQ read counts), the value
is estimated and explicitly labelled `exact: false`. Silently presenting an
estimate as fact is worse than not showing it: someone will put it in a paper.

All functions are synchronous and cancellable; callers run them off the loop.
"""

import gzip
import threading
from pathlib import Path

from app.errors import JobCancelled
from app.logging import get_logger
from app.models import Compression, FormatKind

log = get_logger(__name__)

# How much of a FASTQ/FASTA to sample when estimating record counts.
SAMPLE_RECORDS = 1000
SAMPLE_BYTES_CAP = 8 * 1024 * 1024
# Contig lists can run to hundreds of thousands of entries (scaffold-level
# assemblies). Store a bounded sample plus the true count.
MAX_STORED_CONTIGS = 50
MAX_STORED_SAMPLES = 100


def _check(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise JobCancelled("Cancelled during header parsing")


def parse(
    path: Path,
    kind: FormatKind,
    compression: Compression,
    *,
    cancel_event: threading.Event | None = None,
    display_name: str | None = None,
) -> dict:
    """Extract facts for a file of known format.

    `display_name` is the user-facing filename. It matters because managed
    blobs are stored under their SHA-256, so `path.name` is a hex digest --
    any convention encoded in the filename (notably the _R1/_R2 mate suffix)
    is only visible here.

    Never raises on malformed input: a corrupt file should mark the object with
    a parse error, not fail ingestion or crash a worker. The exception is
    cancellation, which must propagate.
    """
    name = display_name or path.name
    try:
        if kind in (FormatKind.BAM, FormatKind.SAM, FormatKind.CRAM):
            return _parse_alignment(path, kind, cancel_event)
        if kind in (FormatKind.VCF, FormatKind.BCF):
            return _parse_variant(path, cancel_event)
        if kind is FormatKind.FASTQ:
            return _parse_fastq(path, compression, cancel_event, name)
        if kind is FormatKind.FASTA:
            return _parse_fasta(path, compression, cancel_event)
        if kind in (FormatKind.BED, FormatKind.GFF, FormatKind.GTF):
            return _parse_tabular(path, compression, cancel_event)
        return {}
    except JobCancelled:
        raise
    except Exception as e:  # noqa: BLE001 - a bad file must not break ingest
        log.warning("parse_failed", path=str(path), kind=kind.value, error=str(e))
        return {"parse_error": f"{type(e).__name__}: {e}"}


# --- Alignment formats -------------------------------------------------------


def _parse_alignment(path: Path, kind: FormatKind, cancel) -> dict:
    import pysam

    facts: dict = {}
    mode = {"bam": "rb", "sam": "r", "cram": "rc"}[kind.value]

    # check_sq=False: a headerless or reference-less file should still yield
    # whatever it does have rather than raising.
    with pysam.AlignmentFile(str(path), mode, check_sq=False) as af:
        _check(cancel)
        header = af.header.to_dict()

        hd = header.get("HD", {})
        if hd.get("SO"):
            facts["sort_order"] = hd["SO"]
        if hd.get("VN"):
            facts["sam_version"] = hd["VN"]

        sq = header.get("SQ", [])
        if sq:
            facts["reference_count"] = len(sq)
            facts["reference_names"] = [s.get("SN") for s in sq[:MAX_STORED_CONTIGS]]
            facts["reference_lengths"] = {
                s.get("SN"): s.get("LN") for s in sq[:MAX_STORED_CONTIGS] if s.get("SN")
            }
            total = sum(s.get("LN", 0) for s in sq)
            if total:
                facts["reference_total_length"] = total
            if len(sq) > MAX_STORED_CONTIGS:
                facts["reference_names_truncated"] = True

        rgs = header.get("RG", [])
        if rgs:
            facts["read_group_count"] = len(rgs)
            samples = sorted({rg["SM"] for rg in rgs if rg.get("SM")})
            if samples:
                facts["sample_names"] = samples[:MAX_STORED_SAMPLES]
            platforms = sorted({rg["PL"] for rg in rgs if rg.get("PL")})
            if platforms:
                facts["platforms"] = platforms

        pgs = header.get("PG", [])
        if pgs:
            facts["program_chain"] = [
                p.get("PN") or p.get("ID") for p in pgs[:10] if p.get("PN") or p.get("ID")
            ]

        # An index means random access is possible, which decides whether many
        # downstream tools can run at all.
        facts["has_index"] = _has_index(path, kind)

        # Read length comes from the first few records; scanning further would
        # mean reading the whole file for a number that rarely varies.
        _check(cancel)
        facts.update(_sample_alignment_records(af, cancel))

    # Composition and quality need their own pass over the records, so it runs
    # after the header read rather than sharing the file handle above.
    from app.storage import sequence_stats

    facts.update(
        sequence_stats.alignment_stats(path, kind, cancel_event=cancel)
    )
    return facts


def _sample_alignment_records(af, cancel, limit: int = 100) -> dict:
    lengths: list[int] = []
    paired = False
    seen = 0
    try:
        for rec in af.head(limit):
            seen += 1
            if seen % 25 == 0:
                _check(cancel)
            if rec.query_length:
                lengths.append(rec.query_length)
            if rec.is_paired:
                paired = True
    except (ValueError, OSError):
        # head() needs a readable body; a header-only file is legitimate.
        return {}

    if not lengths:
        return {}
    out = {
        "read_length_min": min(lengths),
        "read_length_max": max(lengths),
        "paired": paired,
    }
    if out["read_length_min"] == out["read_length_max"]:
        out["read_length"] = out["read_length_min"]
    return out


def _has_index(path: Path, kind: FormatKind) -> bool:
    """Whether a sibling index file exists.

    Only meaningful for register-in-place files, which sit in their original
    directory alongside any `.bai`/`.crai`. A managed blob is stored alone
    under its hash, so this is legitimately False there -- indexes for managed
    content will be generated and tracked as their own objects later, not
    discovered on disk.
    """
    suffixes = {
        FormatKind.BAM: (".bai", ".csi"),
        FormatKind.CRAM: (".crai",),
        FormatKind.VCF: (".tbi", ".csi"),
        FormatKind.BCF: (".csi",),
        FormatKind.SAM: (),
    }.get(kind, ())
    return any(
        Path(str(path) + s).exists() or path.with_suffix(s).exists() for s in suffixes
    )


# --- Variant formats ---------------------------------------------------------


def _parse_variant(path: Path, cancel) -> dict:
    import pysam

    facts: dict = {}
    with pysam.VariantFile(str(path)) as vf:
        _check(cancel)
        header = vf.header

        if header.version:
            facts["vcf_version"] = str(header.version)

        samples = list(header.samples)
        facts["sample_count"] = len(samples)
        if samples:
            facts["sample_names"] = samples[:MAX_STORED_SAMPLES]
            if len(samples) > MAX_STORED_SAMPLES:
                facts["sample_names_truncated"] = True

        contigs = list(header.contigs)
        if contigs:
            facts["reference_count"] = len(contigs)
            facts["reference_names"] = contigs[:MAX_STORED_CONTIGS]
            lengths = {
                c: header.contigs[c].length
                for c in contigs[:MAX_STORED_CONTIGS]
                if header.contigs[c].length
            }
            if lengths:
                facts["reference_lengths"] = lengths

        info_keys = list(header.info)
        format_keys = list(header.formats)
        filters = [f for f in header.filters if f != "PASS"]
        if info_keys:
            facts["info_field_count"] = len(info_keys)
            facts["info_fields"] = info_keys[:30]
        if format_keys:
            facts["format_fields"] = format_keys[:30]
        if filters:
            facts["filters"] = filters[:20]

        # A tabix/CSI index is what makes region queries possible.
        facts["has_index"] = any(
            Path(str(path) + s).exists() for s in (".tbi", ".csi")
        )

        _check(cancel)
        facts.update(_sample_variant_records(vf, cancel))

    return facts


def _sample_variant_records(vf, cancel, limit: int = 200) -> dict:
    seen = 0
    types: set[str] = set()
    first_chrom = None
    try:
        for rec in vf:
            if first_chrom is None:
                first_chrom = rec.chrom
            seen += 1
            if seen % 50 == 0:
                _check(cancel)
            if rec.alts:
                ref_len = len(rec.ref or "")
                for alt in rec.alts:
                    if len(alt) == ref_len == 1:
                        types.add("SNV")
                    elif len(alt) > ref_len:
                        types.add("insertion")
                    elif len(alt) < ref_len:
                        types.add("deletion")
                    else:
                        types.add("MNV")
            if seen >= limit:
                break
    except (ValueError, OSError, StopIteration):
        return {}

    if not seen:
        return {"record_count": 0, "record_count_exact": True}
    out: dict = {"variant_types_sampled": sorted(types)} if types else {}
    if first_chrom:
        out["first_contig"] = first_chrom
    if seen < limit:
        # We reached EOF inside the sample window, so this count is the truth.
        out["record_count"] = seen
        out["record_count_exact"] = True
    return out


# --- Sequence formats --------------------------------------------------------


def _open_text(path: Path, compression: Compression):
    if compression in (Compression.GZIP, Compression.BGZF):
        return gzip.open(path, "rt", errors="replace")
    return open(path, errors="replace")


def _parse_fastq(
    path: Path, compression: Compression, cancel, display_name: str | None = None
) -> dict:
    """Sample the head of a FASTQ and extrapolate the record count.

    An exact count means decompressing and scanning the entire file -- minutes
    for a 100 GB FASTQ, to produce a number nobody asked for at upload time. The
    estimate is derived from average bytes-per-record over the first 1000
    records and is always labelled inexact.
    """
    facts: dict = {}
    lengths: list[int] = []
    qual_lengths: list[int] = []
    records = 0
    uncompressed_read = 0
    ids: list[str] = []

    with _open_text(path, compression) as fh:
        while records < SAMPLE_RECORDS and uncompressed_read < SAMPLE_BYTES_CAP:
            header = fh.readline()
            if not header:
                break
            seq = fh.readline()
            plus = fh.readline()
            qual = fh.readline()
            if not qual:
                break
            if not header.startswith("@") or not plus.startswith("+"):
                facts["parse_warning"] = "Record structure is not valid FASTQ"
                break

            records += 1
            uncompressed_read += len(header) + len(seq) + len(plus) + len(qual)
            lengths.append(len(seq.rstrip("\n")))
            qual_lengths.append(len(qual.rstrip("\n")))
            if len(ids) < 3:
                ids.append(header[1:].strip())
            if records % 100 == 0:
                _check(cancel)

    if not records:
        return facts

    facts["read_length_min"] = min(lengths)
    facts["read_length_max"] = max(lengths)
    if facts["read_length_min"] == facts["read_length_max"]:
        facts["read_length"] = facts["read_length_min"]
    facts["sampled_records"] = records
    if ids:
        facts["first_read_ids"] = ids

    # Sequence and quality lines must agree in length; a mismatch means the file
    # is malformed in a way that will break downstream tools.
    if any(sl != ql for sl, ql in zip(lengths, qual_lengths, strict=False)):
        facts["parse_warning"] = "Sequence and quality lengths disagree"

    file_size = path.stat().st_size
    if compression is Compression.NONE:
        avg = uncompressed_read / records
        facts["read_count_estimate"] = int(file_size / avg) if avg else 0
        facts["read_count_exact"] = False
    else:
        # Extrapolating through a compression ratio measured on the first few MB
        # is rough -- ratios drift across a large file -- so it is flagged.
        ratio = _estimate_compression_ratio(path)
        if ratio:
            est_uncompressed = file_size * ratio
            avg = uncompressed_read / records
            facts["read_count_estimate"] = int(est_uncompressed / avg) if avg else 0
            facts["read_count_exact"] = False
            facts["estimate_note"] = "Extrapolated from a compressed sample; approximate"

    _infer_pair_hint(display_name or path.name, facts)

    # Base composition and per-position quality, from a deeper sample than the
    # read-length probe above. Failure here must not lose the facts we already
    # have, so it is additive.
    from app.storage import sequence_stats

    facts.update(
        sequence_stats.fastq_stats(path, compression, cancel_event=cancel)
    )
    return facts


def _estimate_compression_ratio(path: Path, _unused: int = 0) -> float | None:
    """Measured uncompressed:compressed ratio, e.g. 3.5 for typical FASTQ gzip.

    Decompresses a bounded prefix and asks the underlying file object how many
    compressed bytes that consumed. Measuring beats assuming a constant: real
    ratios range from ~2x to ~8x depending on read length and quality encoding.
    """
    try:
        with open(path, "rb") as raw:
            with gzip.GzipFile(fileobj=raw) as gz:
                uncompressed = len(gz.read(SAMPLE_BYTES_CAP))
            compressed = raw.tell()
        if uncompressed <= 0 or compressed <= 0:
            return None
        return uncompressed / compressed
    except (OSError, EOFError):
        return None


def _infer_pair_hint(filename: str, facts: dict) -> None:
    """Recognize the _R1/_R2 convention so pairs can be matched later.

    Takes a filename rather than a path: managed blobs live under their hash,
    so the meaningful name is the one the user gave the object.
    """
    name = filename.lower()
    for token, hint in ((("_r1", ".r1", "_1."), "R1"), (("_r2", ".r2", "_2."), "R2")):
        if any(t in name for t in token):
            facts["paired_hint"] = hint
            return


def _parse_fasta(path: Path, compression: Compression, cancel) -> dict:
    """Count sequences exactly for small files, estimate for large ones."""
    facts: dict = {}
    file_size = path.stat().st_size
    # A reference genome FASTA is a few GB; counting '>' lines across that is
    # cheap relative to a FASTQ scan, but not free. Cap it.
    exact_limit = 256 * 1024 * 1024

    names: list[str] = []
    count = 0
    total_bases = 0
    read_bytes = 0
    truncated = False

    with _open_text(path, compression) as fh:
        for line in fh:
            read_bytes += len(line)
            if line.startswith(">"):
                count += 1
                if len(names) < MAX_STORED_CONTIGS:
                    names.append(line[1:].strip().split()[0] if line[1:].strip() else "")
            else:
                total_bases += len(line.rstrip("\n"))
            if count % 500 == 0:
                _check(cancel)
            if compression is Compression.NONE and read_bytes > exact_limit:
                truncated = True
                break

    if truncated:
        facts["sequence_count_estimate"] = int(count * (file_size / read_bytes))
        facts["sequence_count_exact"] = False
    else:
        facts["sequence_count"] = count
        facts["sequence_count_exact"] = True
        facts["total_bases"] = total_bases

    if names:
        facts["sequence_names"] = names
        if count > MAX_STORED_CONTIGS:
            facts["sequence_names_truncated"] = True

    from app.storage import sequence_stats

    facts.update(
        sequence_stats.fasta_stats(path, compression, cancel_event=cancel)
    )
    return facts


def _parse_tabular(path: Path, compression: Compression, cancel) -> dict:
    """BED/GFF/GTF: column shape, track lines, and the contigs seen up front."""
    facts: dict = {}
    contigs: list[str] = []
    seen: set[str] = set()
    data_lines = 0
    comment_lines = 0
    columns: set[int] = set()

    with _open_text(path, compression) as fh:
        for i, line in enumerate(fh):
            if i > 5000:
                break
            if i % 500 == 0:
                _check(cancel)
            stripped = line.rstrip("\n")
            if not stripped:
                continue
            if stripped.startswith(("#", "track", "browser")):
                comment_lines += 1
                continue
            cols = stripped.split("\t")
            columns.add(len(cols))
            data_lines += 1
            if cols[0] not in seen and len(contigs) < MAX_STORED_CONTIGS:
                seen.add(cols[0])
                contigs.append(cols[0])

    if data_lines:
        facts["sampled_records"] = data_lines
        facts["column_counts"] = sorted(columns)
        if len(columns) > 1:
            facts["parse_warning"] = "Rows have inconsistent column counts"
    if comment_lines:
        facts["header_lines"] = comment_lines
    if contigs:
        facts["reference_names"] = contigs
    return facts
