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
# A reference genome FASTA is a few GB; counting '>' lines and scanning bases
# across that is cheap relative to a FASTQ scan, but not free. Cap it. Module
# level rather than a local so a test can patch it down instead of writing a
# 256 MB fixture to exercise the truncation path.
FASTA_EXACT_LIMIT = 256 * 1024 * 1024
# The Nx curve is stored at fixed resolution rather than as raw contig
# lengths: a fragmented draft is hundreds of thousands of contigs, fact
# documents live in Mongo, and Mongo caps a document at 16MB. A hundred
# points costs the same for an 8-contig finished genome and a 500,000-contig
# draft, and it is exactly what the chart draws.
NX_CURVE_POINTS = 100


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
        if kind is FormatKind.GFA:
            return _parse_gfa(path, compression, cancel_event)
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
            # A program invoked several times gets one PG line per invocation;
            # the chain is more readable as the distinct set of tools used, in
            # first-use order.
            names = dict.fromkeys(
                name for p in pgs if (name := p.get("PN") or p.get("ID"))
            )
            facts["program_chain"] = list(names)[:10]

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


def _contiguity_stats(lengths: list[int], gap_bases: int, gap_count: int) -> dict:
    """N50/N90/L50/auN, the Nx curve, and gap counts, over every record's
    true length.

    Genuinely absent from anywhere else now that `assembly_runner._n50` is
    gone: Flye's own table gave an N50 for its own output, and nothing gave
    one for an uploaded assembly. This is the one contiguity fact set that
    applies to any FASTA regardless of what produced it.

    Takes the full length list rather than the capped `sequence_lengths`
    dict, deliberately -- N50 over 50 stored contigs of a 40,000-contig draft
    is not an approximate N50, it is a different number computed from the
    wrong population.

    `sequence_nx_curve` is the continuous form of the same walk: N50 is its
    x=50 point and N90 its x=90 point, computed here in one pass so the three
    can never disagree the way two separate implementations eventually would.
    """
    if not lengths:
        return {}
    ordered = sorted(lengths, reverse=True)
    total = sum(ordered)
    facts: dict = {}
    for label, fraction in (("n50", 0.5), ("n90", 0.9)):
        threshold = total * fraction
        running = 0
        for i, length in enumerate(ordered):
            running += length
            if running >= threshold:
                facts[f"sequence_{label}"] = length
                if label == "n50":
                    facts["sequence_l50"] = i + 1
                break

    # One pass for all 100 thresholds: walk the sorted lengths once, and
    # every time the running total crosses the next percentage boundary,
    # record the contig that carried it there. A per-point rescan would be
    # 100 walks of a list that can hold half a million entries.
    curve: list[list[int]] = []
    running = 0
    x = 1
    for length in ordered:
        running += length
        while x <= NX_CURVE_POINTS and running >= total * x / NX_CURVE_POINTS:
            curve.append([x, length])
            x += 1
        if x > NX_CURVE_POINTS:
            break
    # Floating-point comparison can leave the final point unreached when the
    # running total lands a hair under the threshold it should have met
    # exactly. The last contig is the answer for any remaining x by
    # definition, so fill rather than emit a short curve.
    while x <= NX_CURVE_POINTS:
        curve.append([x, ordered[-1]])
        x += 1
    facts["sequence_nx_curve"] = curve

    # auN: the area under the Nx curve, treating each base as weighted by the
    # length of the contig it sits in. Unlike N50 it does not jump
    # discontinuously when one contig crosses the halfway point, which is why
    # two assemblies can share an N50 and still differ here.
    facts["sequence_auN"] = round(sum(length * length for length in ordered) / total, 1)
    facts["sequence_gap_count"] = gap_count
    facts["sequence_gap_bases"] = gap_bases
    return facts


def _parse_fasta(path: Path, compression: Compression, cancel) -> dict:
    """Count sequences exactly for small files, estimate for large ones."""
    facts: dict = {}
    file_size = path.stat().st_size

    names: list[str] = []
    lengths: dict[str, int] = {}
    # Every record's length, uncapped -- unlike `lengths` above, which is
    # bounded to MAX_STORED_CONTIGS for the fact document. N50 needs the true
    # population; a fragmented draft is a few hundred thousand ints, which is
    # bounded and cheap for the duration of one parse.
    all_lengths: list[int] = []
    count = 0
    total_bases = 0
    total_gap_bases = 0
    total_gap_count = 0
    read_bytes = 0
    truncated = False

    # Per-record accumulation. The current record is only committed when the
    # next header proves it complete, so a record cut by the byte limit is
    # never reported at a truncated length.
    current_name: str | None = None
    current_len = 0
    in_gap = False
    longest: tuple[str, int] | None = None
    shortest: tuple[str, int] | None = None

    def commit() -> None:
        nonlocal longest, shortest
        if current_name is None:
            return
        if len(lengths) < MAX_STORED_CONTIGS:
            lengths[current_name] = current_len
        all_lengths.append(current_len)
        # Extremes track every record, not just the stored window: capping them
        # would report the wrong longest contig for most real assemblies.
        if longest is None or current_len > longest[1]:
            longest = (current_name, current_len)
        if shortest is None or current_len < shortest[1]:
            shortest = (current_name, current_len)

    with _open_text(path, compression) as fh:
        for line in fh:
            read_bytes += len(line)
            if line.startswith(">"):
                commit()
                count += 1
                name = line[1:].strip().split()[0] if line[1:].strip() else ""
                if len(names) < MAX_STORED_CONTIGS:
                    names.append(name)
                current_name = name
                current_len = 0
                in_gap = False
            else:
                seq = line.rstrip("\n")
                n = len(seq)
                total_bases += n
                current_len += n
                # N/n runs are gaps -- scaffolded contigs use them to mark
                # unresolved joins. Counted per-run, not per-base, so a
                # 10,000-N spacer is one gap and not ten thousand. A run can
                # span a line boundary, which is why this is a running
                # per-record flag rather than reset each line.
                for base in seq:
                    is_n = base == "N" or base == "n"
                    if is_n:
                        total_gap_bases += 1
                        if not in_gap:
                            total_gap_count += 1
                    in_gap = is_n
            if count % 500 == 0:
                _check(cancel)
            if compression is Compression.NONE and read_bytes > FASTA_EXACT_LIMIT:
                truncated = True
                break

    if truncated:
        # The in-progress record is mid-sequence and every later record is
        # unseen, so it is dropped rather than committed at a partial length.
        facts["sequence_count_estimate"] = int(count * (file_size / read_bytes))
        facts["sequence_count_exact"] = False
        facts["sequence_lengths_partial"] = True
    else:
        commit()
        facts["sequence_count"] = count
        facts["sequence_count_exact"] = True
        facts["total_bases"] = total_bases
        # Contiguity is never extrapolated, for the same reason the extremes
        # below never are: there is no sound way to guess an N50 from a byte
        # ratio, and a flagged-partial contiguity fact is still the wrong
        # number computed from the wrong population, not an estimate of the
        # right one.
        facts.update(_contiguity_stats(all_lengths, total_gap_bases, total_gap_count))

    if names:
        facts["sequence_names"] = names
        if count > MAX_STORED_CONTIGS:
            facts["sequence_names_truncated"] = True

    if lengths:
        facts["sequence_lengths"] = lengths
    # Emitted even when partial -- they are the true extremes of what was
    # parsed, and sequence_lengths_partial marks them as not final. Lengths are
    # never extrapolated: there is no sound way to guess an unseen contig's
    # length from a byte ratio.
    if longest is not None:
        facts["sequence_longest"] = {"name": longest[0], "length": longest[1]}
    if shortest is not None:
        facts["sequence_shortest"] = {"name": shortest[0], "length": shortest[1]}

    from app.storage import sequence_stats

    facts.update(
        sequence_stats.fasta_stats(path, compression, cancel_event=cancel)
    )
    return facts


def _parse_gfa(path: Path, compression: Compression, cancel) -> dict:
    """Segment and link counts for an assembly graph.

    The two numbers that say what the graph is: how many pieces, and how
    tangled. A graph with as many links as segments is a resolved assembly; one
    with far more is where the contigs came from a repeat-riddled region.

    Segment lengths come from the `LN:i:` tag when present and the sequence
    field otherwise -- Flye writes both, but a GFA is allowed to carry `*` in
    place of the sequence, and reading that as a zero-length contig would make
    a valid graph look empty.
    """
    facts: dict = {}
    segments = 0
    links = 0
    total_length = 0
    read_bytes = 0
    truncated = False
    # Same budget and reasoning as the FASTA path: a graph for a fragmented
    # draft is large, and the counts are worth more than exactness on a file
    # nobody will read to the end.
    limit = 256 * 1024 * 1024

    with _open_text(path, compression) as fh:
        for i, line in enumerate(fh):
            read_bytes += len(line)
            cols = line.rstrip("\n").split("\t")
            if cols[0] == "S":
                segments += 1
                length = None
                for col in cols[3:]:
                    if col.startswith("LN:i:"):
                        try:
                            length = int(col[5:])
                        except ValueError:
                            length = None
                        break
                if length is None and len(cols) > 2 and cols[2] != "*":
                    length = len(cols[2])
                total_length += length or 0
            elif cols[0] == "L":
                links += 1
            if i % 5000 == 0:
                _check(cancel)
            if compression is Compression.NONE and read_bytes > limit:
                truncated = True
                break

    if segments:
        facts["gfa_segment_count"] = segments
        facts["gfa_link_count"] = links
        if total_length:
            facts["gfa_total_length"] = total_length
        if truncated:
            facts["gfa_counts_partial"] = True
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
