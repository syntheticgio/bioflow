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

# Insert size is binned at 10 bp resolution up to 2 kb -- fine enough to see a
# library-prep problem's characteristic shape, coarse enough that the array
# stays small regardless of how many reads are sampled.
INSERT_SIZE_BIN_WIDTH = 10
INSERT_SIZE_MAX = 2000

# Read length is binned at the same 10 bp resolution as insert size, but with
# no ceiling: insert size has a real biological cap from library prep, while
# PacBio HiFi reads routinely exceed 20 kb and a cap would flatten the exact
# shape long-read users need to see.
READ_LENGTH_BIN_WIDTH = 10

# STAR does not use the phred-like scale the other four aligners here do. It
# writes 255 for a uniquely mapped read, and 3, 1 or 0 for a read placed at 2,
# 3-4 or 5+ loci -- ordinal codes for locus count, not qualities. Averaging
# them produces ~247 for a good STAR run against ~50 for the identical reads
# through bwa-mem2, which reads as a dramatically better alignment when it is
# only a different encoding.
#
# 255 is the detection signal because the SAM spec reserves it for "mapping
# quality unavailable", so a phred-scale aligner does not emit it (bwa-mem2,
# minimap2 and hisat2 cap at 60, bowtie2 at 42). Reading it from the records
# rather than from the run's `aligned_by` provenance means an imported STAR
# BAM -- which has no provenance to read -- is recognized the same way.
STAR_MAPQ_UNIQUE = 255

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
    # Binned at integer GC% rather than kept as a list of floats. The
    # distribution is what the chart draws and the mean is derived from the
    # same counts, so the per-read values themselves are never needed -- and a
    # 200k-read sample previously held a 200k-element float list purely to
    # average it.
    gc_histogram: Counter[int] = Counter()
    min_score = 256
    max_score = -1
    length_histogram: Counter[int] = Counter()

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

                bucket = (len(seq) // READ_LENGTH_BIN_WIDTH) * READ_LENGTH_BIN_WIDTH
                length_histogram[bucket] += 1

                gc = seq.count("G") + seq.count("C")
                acgt = gc + seq.count("A") + seq.count("T")
                total_gc += gc
                total_acgt += acgt
                # Reads with no A/C/G/T at all (all-N) are skipped: they have
                # no GC ratio, and binning them at 0% would invent a peak out
                # of unsequenced bases.
                if acgt:
                    gc_histogram[round(100.0 * gc / acgt)] += 1

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
    if gc_histogram:
        binned = sorted(gc_histogram.items())
        facts["gc_per_read_histogram"] = [
            {"gc_percent": pct, "count": n} for pct, n in binned
        ]
        total = sum(gc_histogram.values())
        facts["gc_per_read_mean"] = round(
            sum(pct * n for pct, n in binned) / total, 2
        )

    quality = _quality_curve(qual_sum, qual_n, offset)
    if quality:
        facts["quality_per_position"] = quality
        scores = [q["mean"] for q in quality]
        facts["mean_quality"] = round(sum(scores) / len(scores), 2)
        facts["min_position_quality"] = round(min(scores), 2)

    if length_histogram:
        facts["read_length_histogram"] = [
            {"length_bin": length_bin, "count": n}
            for length_bin, n in sorted(length_histogram.items())
        ]

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

    When a .bai index is present the sample is drawn per contig in proportion
    to contig length, so the derived statistics describe the whole library
    rather than the head of the first contig. Unindexed files keep the cheaper
    head-sample behaviour.

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
    mapq_histogram: Counter[int] = Counter()
    insert_size_histogram: Counter[int] = Counter()
    length_histogram: Counter[int] = Counter()
    saw_paired = False

    def _count_read(rec) -> bool:
        """Process one alignment record, updating accumulators in place.

        Returns True if the read was counted, False if it was skipped
        (secondary/supplementary or no sequence).
        """
        nonlocal reads, mapped, duplicates, total_gc, total_acgt
        nonlocal mapq_sum, mapq_n, saw_paired

        if rec.is_secondary or rec.is_supplementary:
            return False

        seq = rec.query_sequence
        if not seq:
            return False

        reads += 1
        length_bucket = (len(seq) // READ_LENGTH_BIN_WIDTH) * READ_LENGTH_BIN_WIDTH
        length_histogram[length_bucket] += 1
        if not rec.is_unmapped:
            mapped += 1
            mapq_sum += rec.mapping_quality
            mapq_n += 1
            mapq_histogram[rec.mapping_quality] += 1
        if rec.is_duplicate:
            duplicates += 1

        if rec.is_paired:
            saw_paired = True
            # template_length is signed (mate orientation); only
            # positive values are counted so each pair's fragment size
            # is tallied once rather than twice (the mate's own record
            # reports the same length negated).
            tlen = rec.template_length
            if tlen > 0:
                capped = min(tlen, INSERT_SIZE_MAX)
                bucket = (capped // INSERT_SIZE_BIN_WIDTH) * INSERT_SIZE_BIN_WIDTH
                insert_size_histogram[bucket] += 1

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

        return True

    try:
        with pysam.AlignmentFile(str(path), mode, check_sq=False) as af:
            # Check whether a .bai index is available for strided sampling.
            # Any failure (e.g. "AlignmentFile.mapped only available in bam
            # files" for SAM, or no index file present) means we fall back to
            # head sampling rather than strided.
            try:
                af.check_index()
                has_index = af.has_index()
            except Exception:
                has_index = False

            if has_index and af.nreferences > 0:
                # Strided sampling: draw reads per contig in proportion to
                # contig length, so every chromosome contributes to the
                # derived statistics rather than only the head of the first.
                references = af.references
                lengths = af.lengths
                total_length = sum(lengths)

                # Calculate proportional read targets per contig.
                contig_targets: dict[str, int] = {}
                remaining = max_reads
                for ref, length in zip(references, lengths, strict=False):
                    target = max(1, int(max_reads * length / total_length))
                    contig_targets[ref] = target
                    remaining -= target

                # Distribute any residual reads to the longest contig so the
                # total matches max_reads exactly.
                if remaining > 0 and references:
                    contig_targets[references[0]] += remaining

                for ref, target in contig_targets.items():
                    if reads >= max_reads:
                        break
                    reads_this_contig = 0
                    for rec in af.fetch(ref):
                        if reads >= max_reads:
                            break
                        if not _count_read(rec):
                            continue
                        reads_this_contig += 1
                        if reads_this_contig >= target:
                            break
                        if reads % CANCEL_CHECK_READS == 0 and cancel_event is not None:
                            if cancel_event.is_set():
                                raise JobCancelled(
                                    "Cancelled during alignment statistics"
                                )

                # Sample unmapped reads from the unmapped portion at the end
                # of the file, using a small fixed budget so they contribute
                # to mapped_percent realistically.  Using fetch('*') selects
                # only unmapped records via the index rather than scanning the
                # entire file.
                if reads < max_reads:
                    try:
                        unmapped_iter = af.fetch("*")
                    except (ValueError, OSError):
                        # A BAM sorted without an unmapped-read bucket may
                        # not have '*' as a reference; fall back to scanning
                        # to EOF for unmapped reads.
                        unmapped_iter = af.fetch(until_eof=True)
                    for rec in unmapped_iter:
                        if reads >= max_reads:
                            break
                        if not rec.is_unmapped:
                            continue
                        if not _count_read(rec):
                            continue
                        if reads >= max_reads:
                            break
                        if reads % CANCEL_CHECK_READS == 0 and cancel_event is not None:
                            if cancel_event.is_set():
                                raise JobCancelled(
                                    "Cancelled during alignment statistics"
                                )

                sample_method = "strided"
            else:
                # Head sampling: read from the start until max_reads is
                # reached. For unindexed files this is the only option.
                for rec in af:
                    if reads >= max_reads:
                        break
                    _count_read(rec)
                    if reads % CANCEL_CHECK_READS == 0 and cancel_event is not None:
                        if cancel_event.is_set():
                            raise JobCancelled("Cancelled during alignment statistics")
                sample_method = "head"
    except JobCancelled:
        raise
    except (ValueError, OSError) as e:
        # A header-only or truncated file is legitimate; report nothing rather
        # than failing the whole ingest.
        log.warning("alignment_stats_failed", path=str(path), error=str(e))
        return {}

    if reads == 0:
        return {}

    facts: dict = {
        "stats_sampled_reads": reads,
        "stats_sampling": sample_method,
    }

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

    if mapq_histogram:
        facts["mapq_histogram"] = [
            {"mapq": mapq, "count": n} for mapq, n in sorted(mapq_histogram.items())
        ]

    if mapq_n:
        if STAR_MAPQ_UNIQUE in mapq_histogram:
            # Not a phred scale, so neither the mean nor the histogram's
            # x-axis means what it does for every other aligner here. See
            # STAR_MAPQ_UNIQUE. The fraction the codes actually assert is
            # reported instead of a mean over them.
            facts["mapq_scale"] = "star"
            facts["uniquely_mapped_percent"] = round(
                100.0 * mapq_histogram[STAR_MAPQ_UNIQUE] / mapq_n, 2
            )
        else:
            facts["mean_mapping_quality"] = round(mapq_sum / mapq_n, 2)
    # Absent rather than a bucket of zeros for an unpaired BAM, so the
    # frontend can tell "unpaired" from "measured as zero".
    if saw_paired and insert_size_histogram:
        facts["insert_size_histogram"] = [
            {"insert_size": size, "count": n}
            for size, n in sorted(insert_size_histogram.items())
        ]

    if length_histogram:
        facts["read_length_histogram"] = [
            {"length_bin": length_bin, "count": n}
            for length_bin, n in sorted(length_histogram.items())
        ]

    return facts


_COMPLEMENT = str.maketrans("ACGTNacgtn", "TGCANtgcan")


def _revcomp(seq: str) -> str:
    return seq.translate(_COMPLEMENT)[::-1]
