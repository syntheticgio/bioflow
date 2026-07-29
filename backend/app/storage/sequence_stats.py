"""Base composition and per-position quality for sequence files.

Both statistics are computed from a bounded sample rather than the whole file.
That is not a compromise: base composition converges to within ~0.3% by 50k
reads, and per-position quality curves are equally stable. A full scan of a
30 GB FASTQ would cost many minutes of I/O per upload to shift a number in the
third decimal place.

Everything sampled is labelled as sampled -- a composition chart that looks
authoritative but was measured on 0.1% of the file is worse than one that says
so.
"""

import threading
from collections import Counter
from pathlib import Path

from app.errors import JobCancelled
from app.logging import get_logger
from app.models import Compression

log = get_logger(__name__)

# Enough for composition to converge (~1.5s on an uncompressed 377 MB FASTQ);
# small enough that a 30 GB file costs the same, since we stop early either way.
DEFAULT_SAMPLE_READS = 200_000
# Per-position arrays are allocated to this width. Long-read platforms produce
# reads far longer than this, but the quality curve's useful detail is at the
# start and we would otherwise allocate megabytes per file.
MAX_POSITIONS = 1_000
CANCEL_CHECK_READS = 20_000

# Blocks a strided FASTA sample is spread across. Enough to cross every
# chromosome of a human reference, while keeping each block large enough that
# per-seek overhead stays negligible against the bytes read.
FASTA_SAMPLE_BLOCKS = 100

# Phred+33 is effectively universal now. Phred+64 (old Illumina) would report
# implausibly high scores, which we detect rather than silently mis-scale.
PHRED_OFFSET = 33
PHRED64_OFFSET = 64


def fastq_stats(
    path: Path,
    compression: Compression,
    *,
    cancel_event: threading.Event | None = None,
    max_reads: int = DEFAULT_SAMPLE_READS,
) -> dict:
    """Base composition and per-position mean quality from a FASTQ sample."""
    import gzip

    opener = (
        gzip.open
        if compression in (Compression.GZIP, Compression.BGZF)
        else open
    )

    counts: Counter[str] = Counter()
    # Parallel arrays indexed by position: running sum and n, so the mean can
    # be computed without holding every score.
    qual_sum = [0] * MAX_POSITIONS
    qual_n = [0] * MAX_POSITIONS
    reads = 0
    total_gc = 0
    total_acgt = 0
    per_read_gc: list[float] = []
    min_score = 256
    max_score = -1

    try:
        with opener(path, "rt", errors="replace") as fh:
            while reads < max_reads:
                header = fh.readline()
                if not header:
                    break
                seq = fh.readline().rstrip("\n")
                fh.readline()  # '+' separator
                qual = fh.readline().rstrip("\n")
                if not qual:
                    break

                reads += 1
                counts.update(seq)

                gc = seq.count("G") + seq.count("C")
                acgt = gc + seq.count("A") + seq.count("T")
                total_gc += gc
                total_acgt += acgt
                if acgt:
                    per_read_gc.append(100.0 * gc / acgt)

                for i, ch in enumerate(qual[:MAX_POSITIONS]):
                    score = ord(ch)
                    qual_sum[i] += score
                    qual_n[i] += 1
                    if score < min_score:
                        min_score = score
                    if score > max_score:
                        max_score = score

                if reads % CANCEL_CHECK_READS == 0 and cancel_event is not None:
                    if cancel_event.is_set():
                        raise JobCancelled("Cancelled during sequence statistics")
    except JobCancelled:
        raise
    except (OSError, EOFError, UnicodeDecodeError) as e:
        log.warning("fastq_stats_failed", path=str(path), error=str(e))
        return {}

    if reads == 0:
        return {}

    facts: dict = {"stats_sampled_reads": reads}

    # Phred+64 scores start at 64; a minimum well above the Phred+33 range is
    # the standard way to tell the encodings apart.
    offset = PHRED_OFFSET
    if min_score >= 64 and max_score > 74:
        offset = PHRED64_OFFSET
        facts["quality_encoding"] = "Phred+64"
    elif max_score >= 0:
        facts["quality_encoding"] = "Phred+33"

    composition = _composition(counts)
    if composition:
        facts["base_composition"] = composition
    if total_acgt:
        facts["gc_content_percent"] = round(100.0 * total_gc / total_acgt, 2)
    if per_read_gc:
        facts["gc_per_read_mean"] = round(sum(per_read_gc) / len(per_read_gc), 2)

    quality = _quality_curve(qual_sum, qual_n, offset)
    if quality:
        facts["quality_per_position"] = quality
        scores = [q["mean"] for q in quality]
        facts["mean_quality"] = round(sum(scores) / len(scores), 2)
        facts["min_position_quality"] = round(min(scores), 2)

    return facts


def fasta_stats(
    path: Path,
    compression: Compression,
    *,
    cancel_event: threading.Event | None = None,
    max_bases: int = 50_000_000,
) -> dict:
    """Base composition for a FASTA file (no quality scores to report).

    The cap is a performance guard, not a compromise on correctness -- but a
    capped read taken from the front of a multi-GB reference describes chr1,
    not the assembly, and GC varies enough between chromosomes to mislead. For
    seekable (uncompressed) files the same byte budget is instead spread across
    the whole file. Gzip and BGZF cannot seek cheaply, so they keep the prefix
    read and say so in `stats_sampling`.
    """
    import gzip

    is_compressed = compression in (Compression.GZIP, Compression.BGZF)
    file_size = path.stat().st_size

    counts: Counter[str] = Counter()

    try:
        # Striding needs at least one byte of headroom per block to make
        # distinct seeks; below that, `stride = file_size // FASTA_SAMPLE_BLOCKS`
        # would truncate to 0 and every block would re-read the same bytes.
        if (
            not is_compressed
            and file_size > max_bases
            and file_size >= FASTA_SAMPLE_BLOCKS
        ):
            seen, mode = _fasta_sample_strided(
                path, counts, file_size, max_bases, cancel_event
            )
        else:
            opener = gzip.open if is_compressed else open
            with opener(path, "rt", errors="replace") as fh:
                seen = _fasta_read_block(fh, counts, max_bases, cancel_event)
            # A file smaller than the budget was read end to end: the figure is
            # exact, not an estimate, and should not carry a "sampled" caveat.
            mode = "prefix" if seen >= max_bases else "complete"
    except JobCancelled:
        raise
    except (OSError, EOFError, UnicodeDecodeError) as e:
        log.warning("fasta_stats_failed", path=str(path), error=str(e))
        return {}

    if seen == 0:
        return {}

    facts: dict = {"stats_sampled_bases": seen, "stats_sampling": mode}
    composition = _composition(counts)
    if composition:
        facts["base_composition"] = composition

    gc = counts.get("G", 0) + counts.get("C", 0) + counts.get("g", 0) + counts.get("c", 0)
    acgt = gc + sum(counts.get(b, 0) for b in "ATat")
    if acgt:
        facts["gc_content_percent"] = round(100.0 * gc / acgt, 2)
    return facts


def _fasta_read_block(
    fh,
    counts: Counter[str],
    budget: int,
    cancel_event: threading.Event | None,
) -> int:
    """Count bases from the current handle position until `budget` is reached.

    Returns the number of bases counted. Header lines are skipped and do not
    consume budget.
    """
    seen = 0
    lines = 0
    for line in fh:
        lines += 1
        if line.startswith(">"):
            continue
        seq = line.rstrip("\n")
        counts.update(seq)
        seen += len(seq)
        if seen >= budget:
            break
        if lines % 100_000 == 0 and cancel_event is not None:
            if cancel_event.is_set():
                raise JobCancelled("Cancelled during sequence statistics")
    return seen


def _fasta_sample_strided(
    path: Path,
    counts: Counter[str],
    file_size: int,
    max_bases: int,
    cancel_event: threading.Event | None,
) -> tuple[int, str]:
    """Spend the byte budget in equal blocks spread across the file.

    Same total bytes read as a prefix scan, so the same cost -- but the sample
    crosses every chromosome instead of stopping inside the first. Callers
    must ensure `file_size >= FASTA_SAMPLE_BLOCKS` or the stride below
    truncates to 0 and every block collapses onto the same offset.
    """
    per_block = max(1, max_bases // FASTA_SAMPLE_BLOCKS)
    stride = file_size // FASTA_SAMPLE_BLOCKS

    seen = 0
    with open(path, errors="replace") as fh:
        for i in range(FASTA_SAMPLE_BLOCKS):
            if cancel_event is not None and cancel_event.is_set():
                raise JobCancelled("Cancelled during sequence statistics")
            fh.seek(i * stride)
            if i > 0:
                # A seek lands mid-line: that partial line would start counting
                # from an arbitrary column, and could be the tail of a header.
                # Discard it and start clean on the next line boundary.
                fh.readline()
            seen += _fasta_read_block(fh, counts, per_block, cancel_event)
    return seen, "strided"


def _composition(counts: Counter[str]) -> list[dict]:
    """Normalize to A/C/G/T/N plus an 'Other' bucket for IUPAC ambiguity codes.

    Case is folded: soft-masked reference sequence uses lowercase for repeats,
    which is a masking annotation rather than a different base.
    """
    folded: Counter[str] = Counter()
    for base, n in counts.items():
        upper = base.upper()
        if upper in "ACGTN":
            folded[upper] += n
        elif upper.isalpha():
            folded["Other"] += n

    total = sum(folded.values())
    if not total:
        return []

    order = ["A", "C", "G", "T", "N", "Other"]
    return [
        {
            "base": b,
            "count": folded[b],
            "percent": round(100.0 * folded[b] / total, 3),
        }
        for b in order
        if folded.get(b)
    ]


def _quality_curve(
    qual_sum: list[int], qual_n: list[int], offset: int
) -> list[dict]:
    """Mean Phred score per position, truncated at the last observed position."""
    curve = []
    for i, n in enumerate(qual_n):
        if n == 0:
            break
        curve.append(
            {
                "position": i + 1,
                "mean": round(qual_sum[i] / n - offset, 2),
                "count": n,
            }
        )
    return curve


def alignment_stats(
    path: Path,
    kind,
    *,
    cancel_event: threading.Event | None = None,
    max_reads: int = DEFAULT_SAMPLE_READS,
) -> dict:
    """Base composition and per-position quality from a BAM/CRAM/SAM sample.

    Decoding alignment records is more expensive than reading FASTQ text, but
    measured at ~300k records/sec -- a 200k sample costs well under a second,
    so the same budget applies.

    Reverse-strand reads are stored as the reverse complement of what the
    sequencer produced. Both the sequence and the quality string are therefore
    flipped back before counting, or the per-position curve would be the
    average of two opposing gradients and the composition would be inverted for
    half the reads.
    """
    import pysam

    mode = {"bam": "rb", "sam": "r", "cram": "rc"}.get(
        getattr(kind, "value", str(kind)), "rb"
    )

    counts: Counter[str] = Counter()
    qual_sum = [0] * MAX_POSITIONS
    qual_n = [0] * MAX_POSITIONS
    reads = 0
    mapped = 0
    duplicates = 0
    total_gc = 0
    total_acgt = 0
    mapq_sum = 0
    mapq_n = 0

    try:
        with pysam.AlignmentFile(str(path), mode, check_sq=False) as af:
            for rec in af:
                if rec.is_secondary or rec.is_supplementary:
                    # Secondary/supplementary records repeat sequence already
                    # counted from the primary alignment.
                    continue

                seq = rec.query_sequence
                if not seq:
                    continue

                reads += 1
                if not rec.is_unmapped:
                    mapped += 1
                    mapq_sum += rec.mapping_quality
                    mapq_n += 1
                if rec.is_duplicate:
                    duplicates += 1

                quals = rec.query_qualities
                if rec.is_reverse:
                    seq = _revcomp(seq)
                    if quals is not None:
                        quals = list(quals)[::-1]

                counts.update(seq)
                gc = seq.count("G") + seq.count("C")
                total_gc += gc
                total_acgt += gc + seq.count("A") + seq.count("T")

                if quals is not None:
                    for i, score in enumerate(quals[:MAX_POSITIONS]):
                        qual_sum[i] += score
                        qual_n[i] += 1

                if reads % CANCEL_CHECK_READS == 0 and cancel_event is not None:
                    if cancel_event.is_set():
                        raise JobCancelled("Cancelled during alignment statistics")
                if reads >= max_reads:
                    break
    except JobCancelled:
        raise
    except (ValueError, OSError) as e:
        # A header-only or truncated file is legitimate; report nothing rather
        # than failing the whole ingest.
        log.warning("alignment_stats_failed", path=str(path), error=str(e))
        return {}

    if reads == 0:
        return {}

    facts: dict = {"stats_sampled_reads": reads}

    composition = _composition(counts)
    if composition:
        facts["base_composition"] = composition
    if total_acgt:
        facts["gc_content_percent"] = round(100.0 * total_gc / total_acgt, 2)

    # BAM stores Phred scores as integers, so there is no encoding to detect.
    curve = _quality_curve(qual_sum, qual_n, offset=0)
    if curve:
        facts["quality_per_position"] = curve
        scores = [q["mean"] for q in curve]
        facts["mean_quality"] = round(sum(scores) / len(scores), 2)
        facts["min_position_quality"] = round(min(scores), 2)

    facts["mapped_percent"] = round(100.0 * mapped / reads, 2)
    if duplicates:
        facts["duplicate_percent"] = round(100.0 * duplicates / reads, 2)
    if mapq_n:
        facts["mean_mapping_quality"] = round(mapq_sum / mapq_n, 2)

    return facts


_COMPLEMENT = str.maketrans("ACGTNacgtn", "TGCANtgcan")


def _revcomp(seq: str) -> str:
    return seq.translate(_COMPLEMENT)[::-1]
