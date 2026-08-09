"""Base composition and per-position quality.

Fixtures use deliberately skewed compositions so a miscalculation shows up as
an obviously wrong number rather than something plausible.
"""

import gzip
import threading

import pytest

from app.errors import JobCancelled
from app.models import Compression
from app.storage import sequence_stats as ss


def write_fastq(path, reads, seq="AAAACCCGGT", qual="IIIIIHHHFF"):
    with open(path, "w") as f:
        for i in range(reads):
            f.write(f"@read{i}\n{seq}\n+\n{qual}\n")
    return path


class TestComposition:
    def test_percentages_are_exact(self, tmp_path):
        """A=4 C=3 G=2 T=1 per read, so the percentages are unambiguous."""
        r = ss.fastq_stats(write_fastq(tmp_path / "t.fastq", 100), Compression.NONE)
        pct = {c["base"]: c["percent"] for c in r["base_composition"]}
        assert pct == {"A": 40.0, "C": 30.0, "G": 20.0, "T": 10.0}

    def test_counts_match_percentages(self, tmp_path):
        r = ss.fastq_stats(write_fastq(tmp_path / "t.fastq", 100), Compression.NONE)
        counts = {c["base"]: c["count"] for c in r["base_composition"]}
        assert counts == {"A": 400, "C": 300, "G": 200, "T": 100}

    def test_gc_content(self, tmp_path):
        r = ss.fastq_stats(write_fastq(tmp_path / "t.fastq", 50), Compression.NONE)
        assert r["gc_content_percent"] == 50.0

    def test_ambiguity_codes_bucket_to_other(self, tmp_path):
        """IUPAC codes (R, Y, K, M...) are real but too many to chart
        individually; they must be counted, not dropped."""
        p = write_fastq(tmp_path / "t.fastq", 1, seq="ACGTNRYKM", qual="IIIIIIIII")
        counts = {
            c["base"]: c["count"] for c in ss.fastq_stats(p, Compression.NONE)["base_composition"]
        }
        assert counts["Other"] == 4
        assert counts["N"] == 1

    def test_n_is_reported_separately(self, tmp_path):
        """N is a called-but-unknown base and matters for QC, so it is never
        folded into Other."""
        p = write_fastq(tmp_path / "t.fastq", 10, seq="ACGTN", qual="IIIII")
        bases = {c["base"] for c in ss.fastq_stats(p, Compression.NONE)["base_composition"]}
        assert "N" in bases

    def test_zero_count_bases_are_omitted(self, tmp_path):
        """An all-A file should not chart three empty slices."""
        p = write_fastq(tmp_path / "t.fastq", 10, seq="AAAA", qual="IIII")
        bases = [c["base"] for c in ss.fastq_stats(p, Compression.NONE)["base_composition"]]
        assert bases == ["A"]


class TestQualityCurve:
    def test_phred33_is_decoded(self, tmp_path):
        """'I' is ASCII 73; 73 - 33 = Q40."""
        p = write_fastq(tmp_path / "t.fastq", 10, seq="ACGT", qual="IIII")
        r = ss.fastq_stats(p, Compression.NONE)
        assert all(q["mean"] == 40.0 for q in r["quality_per_position"])
        assert r["quality_encoding"] == "Phred+33"

    def test_declining_quality_is_captured(self, tmp_path):
        """Real reads degrade toward the 3' end -- the whole point of the chart."""
        p = write_fastq(tmp_path / "t.fastq", 10, seq="ACGT", qual="IH5#")
        curve = ss.fastq_stats(p, Compression.NONE)["quality_per_position"]
        means = [q["mean"] for q in curve]
        assert means == sorted(means, reverse=True)
        assert means[0] > means[-1]

    def test_curve_length_matches_read_length(self, tmp_path):
        p = write_fastq(tmp_path / "t.fastq", 5, seq="ACGTACGT", qual="IIIIIIII")
        assert len(ss.fastq_stats(p, Compression.NONE)["quality_per_position"]) == 8

    def test_phred64_is_detected(self, tmp_path):
        """Old Illumina encoding would otherwise report impossible Q70+ scores."""
        p = write_fastq(tmp_path / "t.fastq", 10, seq="ACGT", qual="hhhh")
        r = ss.fastq_stats(p, Compression.NONE)
        assert r["quality_encoding"] == "Phred+64"
        assert r["quality_per_position"][0]["mean"] == 40.0

    def test_position_counts_are_reported(self, tmp_path):
        """Ragged read lengths mean later positions have fewer observations;
        the count makes a noisy tail interpretable."""
        p = tmp_path / "t.fastq"
        with open(p, "w") as f:
            f.write("@a\nACGT\n+\nIIII\n")
            f.write("@b\nAC\n+\nII\n")
        curve = ss.fastq_stats(p, Compression.NONE)["quality_per_position"]
        assert curve[0]["count"] == 2
        assert curve[3]["count"] == 1


class TestCompression:
    def test_gzip_gives_identical_results(self, tmp_path):
        plain = write_fastq(tmp_path / "t.fastq", 100)
        gz = tmp_path / "t.fastq.gz"
        gz.write_bytes(gzip.compress(plain.read_bytes()))

        a = ss.fastq_stats(plain, Compression.NONE)
        b = ss.fastq_stats(gz, Compression.GZIP)
        assert a["base_composition"] == b["base_composition"]
        assert a["quality_per_position"] == b["quality_per_position"]


class TestFasta:
    def test_composition(self, tmp_path):
        p = tmp_path / "t.fasta"
        p.write_text(">c1\nAAAACCCGGT\n")
        counts = {
            c["base"]: c["count"] for c in ss.fasta_stats(p, Compression.NONE)["base_composition"]
        }
        assert counts == {"A": 4, "C": 3, "G": 2, "T": 1}

    def test_lowercase_is_folded(self, tmp_path):
        """Soft-masked repeats use lowercase; that is a masking annotation, not
        a different base."""
        p = tmp_path / "t.fasta"
        p.write_text(">c1\nAAaa\n")
        counts = {
            c["base"]: c["count"] for c in ss.fasta_stats(p, Compression.NONE)["base_composition"]
        }
        assert counts == {"A": 4}

    def test_headers_are_not_counted(self, tmp_path):
        p = tmp_path / "t.fasta"
        p.write_text(">ACGTACGT_header_with_bases\nAC\n")
        r = ss.fasta_stats(p, Compression.NONE)
        assert r["stats_sampled_bases"] == 2

    def test_no_quality_curve(self, tmp_path):
        p = tmp_path / "t.fasta"
        p.write_text(">c\nACGT\n")
        assert "quality_per_position" not in ss.fasta_stats(p, Compression.NONE)


class TestSamplingAndLimits:
    def test_sample_size_is_capped(self, tmp_path):
        p = write_fastq(tmp_path / "t.fastq", 500)
        r = ss.fastq_stats(p, Compression.NONE, max_reads=100)
        assert r["stats_sampled_reads"] == 100

    def test_sampled_count_is_always_reported(self, tmp_path):
        """A composition chart that looks authoritative but came from 0.1% of
        the file must say so."""
        r = ss.fastq_stats(write_fastq(tmp_path / "t.fastq", 10), Compression.NONE)
        assert r["stats_sampled_reads"] == 10

    def test_long_reads_are_truncated_not_crashed(self, tmp_path):
        p = write_fastq(
            tmp_path / "t.fastq", 2,
            seq="A" * (ss.MAX_POSITIONS + 500),
            qual="I" * (ss.MAX_POSITIONS + 500),
        )
        curve = ss.fastq_stats(p, Compression.NONE)["quality_per_position"]
        assert len(curve) == ss.MAX_POSITIONS


class TestRobustness:
    def test_empty_file(self, tmp_path):
        p = tmp_path / "empty.fastq"
        p.write_text("")
        assert ss.fastq_stats(p, Compression.NONE) == {}

    def test_truncated_record_does_not_raise(self, tmp_path):
        p = tmp_path / "t.fastq"
        p.write_text("@r\nACGT\n+\n")  # quality line missing
        assert isinstance(ss.fastq_stats(p, Compression.NONE), dict)

    def test_missing_file_returns_empty(self, tmp_path):
        assert ss.fastq_stats(tmp_path / "nope.fastq", Compression.NONE) == {}

    def test_cancellation_propagates(self, tmp_path):
        """Must not be swallowed by the catch-all error handling."""
        p = write_fastq(tmp_path / "t.fastq", ss.CANCEL_CHECK_READS + 100)
        event = threading.Event()
        event.set()
        with pytest.raises(JobCancelled):
            ss.fastq_stats(p, Compression.NONE, cancel_event=event)


class TestParserIntegration:
    def test_fastq_parser_includes_stats(self, tmp_path):
        from app.models import FormatKind
        from app.storage import parsers

        p = write_fastq(tmp_path / "reads.fastq", 100)
        facts = parsers.parse(p, FormatKind.FASTQ, Compression.NONE)
        assert "base_composition" in facts
        assert "quality_per_position" in facts
        # The pre-existing facts must survive the addition.
        assert "read_length" in facts

    def test_fasta_parser_includes_composition(self, tmp_path):
        from app.models import FormatKind
        from app.storage import parsers

        p = tmp_path / "ref.fasta"
        p.write_text(">c1\nACGTACGT\n")
        facts = parsers.parse(p, FormatKind.FASTA, Compression.NONE)
        assert "base_composition" in facts
        assert "sequence_count" in facts


class TestAlignmentStats:
    """BAM/CRAM composition and quality.

    The critical case is reverse-strand handling: aligners store those reads as
    the reverse complement of what the sequencer produced, so both sequence and
    quality must be flipped back before counting.
    """

    @pytest.fixture
    def bam_both_strands(self, tmp_path):
        pysam = pytest.importorskip("pysam")
        p = tmp_path / "t.bam"
        hdr = {"HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 1000}]}
        with pysam.AlignmentFile(str(p), "wb", header=hdr) as out:
            fwd = pysam.AlignedSegment()
            fwd.query_name = "fwd"
            fwd.query_sequence = "AAAACCCGGT"
            fwd.flag = 0
            fwd.reference_id = 0
            fwd.reference_start = 10
            fwd.mapping_quality = 60
            fwd.cigar = [(0, 10)]
            fwd.query_qualities = pysam.qualitystring_to_array("IIIIIHHHFF")
            out.write(fwd)

            # The same original read, stored reverse-complemented.
            rev = pysam.AlignedSegment()
            rev.query_name = "rev"
            rev.query_sequence = "ACCGGGTTTT"
            rev.flag = 16
            rev.reference_id = 0
            rev.reference_start = 20
            rev.mapping_quality = 60
            rev.cigar = [(0, 10)]
            rev.query_qualities = pysam.qualitystring_to_array("FFHHHIIIII")
            out.write(rev)
        return p

    def test_reverse_strand_is_un_reversed(self, bam_both_strands):
        """Both reads are the same original sequence, so the composition must
        be exactly double one read -- not a mix of a sequence and its
        complement."""
        from app.models import FormatKind

        r = ss.alignment_stats(bam_both_strands, FormatKind.BAM)
        counts = {c["base"]: c["count"] for c in r["base_composition"]}
        assert counts == {"A": 8, "C": 6, "G": 4, "T": 2}

    def test_reverse_strand_quality_is_un_reversed(self, bam_both_strands):
        """Without flipping the quality string, the curve would average a
        declining gradient against an ascending one and come out flat."""
        from app.models import FormatKind

        curve = ss.alignment_stats(bam_both_strands, FormatKind.BAM)[
            "quality_per_position"
        ]
        means = [q["mean"] for q in curve]
        assert means[0] == 40.0
        assert means[-1] == 37.0
        assert means == sorted(means, reverse=True)

    def test_bam_quality_needs_no_offset(self, bam_both_strands):
        """BAM stores Phred scores as integers already."""
        from app.models import FormatKind

        r = ss.alignment_stats(bam_both_strands, FormatKind.BAM)
        assert r["quality_per_position"][0]["mean"] == 40.0

    def test_alignment_metrics(self, bam_both_strands):
        from app.models import FormatKind

        r = ss.alignment_stats(bam_both_strands, FormatKind.BAM)
        assert r["mapped_percent"] == 100.0
        assert r["mean_mapping_quality"] == 60.0

    def test_secondary_alignments_are_skipped(self, tmp_path):
        """Secondary records repeat sequence already counted from the primary,
        so counting them would double-count those bases."""
        pysam = pytest.importorskip("pysam")
        from app.models import FormatKind

        p = tmp_path / "sec.bam"
        hdr = {"HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 1000}]}
        with pysam.AlignmentFile(str(p), "wb", header=hdr) as out:
            for flag in (0, 256):  # primary, then secondary
                a = pysam.AlignedSegment()
                a.query_name = "r"
                a.query_sequence = "AAAA"
                a.flag = flag
                a.reference_id = 0
                a.reference_start = 10
                a.cigar = [(0, 4)]
                a.query_qualities = pysam.qualitystring_to_array("IIII")
                out.write(a)

        r = ss.alignment_stats(p, FormatKind.BAM)
        assert r["stats_sampled_reads"] == 1
        assert r["base_composition"][0]["count"] == 4

    def test_header_only_bam_returns_empty(self, tmp_path):
        pysam = pytest.importorskip("pysam")
        from app.models import FormatKind

        p = tmp_path / "empty.bam"
        with pysam.AlignmentFile(
            str(p), "wb", header={"HD": {"VN": "1.6"}, "SQ": [{"SN": "c", "LN": 10}]}
        ):
            pass
        assert ss.alignment_stats(p, FormatKind.BAM) == {}

    def test_parser_integration(self, bam_both_strands):
        """The alignment parser must return header facts and stats together."""
        from app.models import FormatKind
        from app.storage import parsers

        facts = parsers.parse(bam_both_strands, FormatKind.BAM, Compression.BGZF)
        assert "base_composition" in facts
        assert "quality_per_position" in facts
        assert "reference_count" in facts  # pre-existing facts survive


def _write_bam(path, records):
    pysam = pytest.importorskip("pysam")
    hdr = {"HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 1000}]}
    with pysam.AlignmentFile(str(path), "wb", header=hdr) as out:
        for r in records:
            a = pysam.AlignedSegment()
            a.query_name = r["name"]
            seq = r.get("seq", "ACGT")
            a.query_sequence = seq
            a.flag = r.get("flag", 0)
            a.reference_id = 0
            a.reference_start = r.get("pos", 10)
            a.mapping_quality = r.get("mapq", 0)
            a.cigar = [(0, len(seq))]
            a.query_qualities = pysam.qualitystring_to_array("I" * len(seq))
            if "template_length" in r:
                a.template_length = r["template_length"]
            out.write(a)
    return path


class TestMapqHistogram:
    def test_bucketed_by_mapping_quality(self, tmp_path):
        from app.models import FormatKind

        p = _write_bam(
            tmp_path / "mapq.bam",
            [
                {"name": "r1", "mapq": 0},
                {"name": "r2", "mapq": 0},
                {"name": "r3", "mapq": 60},
            ],
        )
        facts = ss.alignment_stats(p, FormatKind.BAM)
        histogram = {h["mapq"]: h["count"] for h in facts["mapq_histogram"]}
        assert histogram[0] == 2
        assert histogram[60] == 1

    def test_unmapped_reads_are_excluded(self, tmp_path):
        """Flag 4 is unmapped; mapping_quality on an unmapped read is not a
        meaningful measurement and must not appear in the histogram."""
        from app.models import FormatKind

        p = _write_bam(
            tmp_path / "unmapped.bam",
            [
                {"name": "r1", "flag": 4, "mapq": 0},
                {"name": "r2", "mapq": 30},
            ],
        )
        facts = ss.alignment_stats(p, FormatKind.BAM)
        total = sum(h["count"] for h in facts["mapq_histogram"])
        assert total == 1


class TestStarMapqScale:
    """STAR's MAPQ is a locus-count code, not a phred score.

    Averaging {0, 1, 3, 255} lands near 250 for a good run, against ~50 for
    the same reads through bwa-mem2 -- so the mean is suppressed in favour of
    the fraction those codes actually assert.
    """

    def _star_bam(self, tmp_path, name="star.bam"):
        # 3 unique, 1 at two loci, 1 at 5+ -- STAR's whole vocabulary.
        return _write_bam(
            tmp_path / name,
            [
                {"name": "r1", "mapq": 255},
                {"name": "r2", "mapq": 255},
                {"name": "r3", "mapq": 255},
                {"name": "r4", "mapq": 3},
                {"name": "r5", "mapq": 0},
            ],
        )

    def test_mean_is_suppressed_on_the_star_scale(self, tmp_path):
        from app.models import FormatKind

        facts = ss.alignment_stats(self._star_bam(tmp_path), FormatKind.BAM)
        assert "mean_mapping_quality" not in facts
        assert facts["mapq_scale"] == "star"

    def test_uniquely_mapped_percent_replaces_it(self, tmp_path):
        from app.models import FormatKind

        facts = ss.alignment_stats(self._star_bam(tmp_path), FormatKind.BAM)
        assert facts["uniquely_mapped_percent"] == 60.0

    def test_histogram_keeps_star_codes_verbatim(self, tmp_path):
        """The codes are the honest record of what STAR wrote; only their
        presentation changes. Rescaling them to look phred-like would make a
        STAR BAM's own numbers unrecognizable against its SAM records."""
        from app.models import FormatKind

        facts = ss.alignment_stats(self._star_bam(tmp_path), FormatKind.BAM)
        histogram = {h["mapq"]: h["count"] for h in facts["mapq_histogram"]}
        assert histogram == {0: 1, 3: 1, 255: 3}

    def test_phred_aligners_are_untouched(self, tmp_path):
        """The detection must not fire on the other four aligners, which cap
        at 60 (bwa-mem2, minimap2, hisat2) or 42 (bowtie2)."""
        from app.models import FormatKind

        p = _write_bam(
            tmp_path / "bwa.bam",
            [{"name": "r1", "mapq": 60}, {"name": "r2", "mapq": 42}, {"name": "r3", "mapq": 0}],
        )
        facts = ss.alignment_stats(p, FormatKind.BAM)
        assert facts["mean_mapping_quality"] == 34.0
        assert "mapq_scale" not in facts
        assert "uniquely_mapped_percent" not in facts

    def test_unmapped_reads_do_not_dilute_the_unique_fraction(self, tmp_path):
        """The denominator is mapped reads, matching the histogram -- an
        unmapped read has no MAPQ to be unique or not."""
        from app.models import FormatKind

        p = _write_bam(
            tmp_path / "star_unmapped.bam",
            [
                {"name": "r1", "mapq": 255},
                {"name": "r2", "mapq": 0},
                {"name": "r3", "flag": 4, "mapq": 0},
            ],
        )
        facts = ss.alignment_stats(p, FormatKind.BAM)
        assert facts["uniquely_mapped_percent"] == 50.0


class TestInsertSizeHistogram:
    def test_positive_template_lengths_are_binned(self, tmp_path):
        from app.models import FormatKind

        p = _write_bam(
            tmp_path / "insert.bam",
            [
                {"name": "r1", "flag": 3, "template_length": 300},
                {"name": "r2", "flag": 3, "template_length": 305},
                {"name": "r3", "flag": 3, "template_length": 500},
            ],
        )
        facts = ss.alignment_stats(p, FormatKind.BAM)
        assert "insert_size_histogram" in facts
        total = sum(h["count"] for h in facts["insert_size_histogram"])
        assert total == 3

    def test_unpaired_reads_produce_no_insert_size_histogram(self, tmp_path):
        """A single-end BAM has no meaningful template length -- absent, not a
        bucket of zeros, so the frontend can tell 'unpaired' from 'measured as
        zero'."""
        from app.models import FormatKind

        p = _write_bam(tmp_path / "unpaired.bam", [{"name": "r1", "flag": 0}])
        facts = ss.alignment_stats(p, FormatKind.BAM)
        assert "insert_size_histogram" not in facts


class TestReadLengthHistogram:
    def test_bucketed_by_10bp_width(self, tmp_path):
        """Two reads of length 10 land in the same 10bp bucket as each other,
        a length-25 read lands in a different bucket."""
        path = tmp_path / "lengths.fastq"
        with open(path, "w") as f:
            f.write("@r1\n" + "A" * 10 + "\n+\n" + "I" * 10 + "\n")
            f.write("@r2\n" + "A" * 10 + "\n+\n" + "I" * 10 + "\n")
            f.write("@r3\n" + "A" * 25 + "\n+\n" + "I" * 25 + "\n")
        facts = ss.fastq_stats(path, Compression.NONE)
        histogram = {h["length_bin"]: h["count"] for h in facts["read_length_histogram"]}
        assert histogram[10] == 2
        assert histogram[20] == 1

    def test_uncapped_for_long_reads(self, tmp_path):
        """Unlike insert_size_histogram's 2kb cap, length has no ceiling --
        PacBio HiFi reads routinely exceed 20kb and must not be clamped."""
        path = tmp_path / "long.fastq"
        with open(path, "w") as f:
            f.write("@r1\n" + "A" * 25_000 + "\n+\n" + "I" * 25_000 + "\n")
        facts = ss.fastq_stats(path, Compression.NONE)
        histogram = {h["length_bin"]: h["count"] for h in facts["read_length_histogram"]}
        assert histogram[25_000] == 1


class TestFastaSampling:
    """GC sampling must describe the file, not just its first chromosome."""

    def write_skewed_fasta(self, path, *, line_len=60, lines_per_half=20_000):
        """High-GC first half, low-GC second half.

        True whole-file GC is 50%: equal halves at 100% and 0%. A prefix read
        that never reaches the second half reports ~100%.
        """
        with open(path, "w") as f:
            f.write(">high_gc\n")
            for _ in range(lines_per_half):
                f.write("GC" * (line_len // 2) + "\n")
            f.write(">low_gc\n")
            for _ in range(lines_per_half):
                f.write("AT" * (line_len // 2) + "\n")
        return path

    def test_strided_sample_spans_the_file(self, tmp_path):
        p = self.write_skewed_fasta(tmp_path / "skewed.fasta")
        # A budget far under the file size forces sampling rather than a full
        # read; the whole point is what happens when we cannot read it all.
        r = ss.fasta_stats(p, Compression.NONE, max_bases=100_000)
        assert r["stats_sampling"] == "strided"
        # True value is 50%. A prefix read gives ~100%, so a generous window
        # still fails loudly on the old behavior.
        assert 40.0 <= r["gc_content_percent"] <= 60.0

    def test_small_file_is_complete_not_sampled(self, tmp_path):
        p = tmp_path / "small.fasta"
        p.write_text(">c1\nGCGCATAT\n")
        r = ss.fasta_stats(p, Compression.NONE)
        assert r["stats_sampling"] == "complete"
        assert r["gc_content_percent"] == 50.0

    def test_gzip_falls_back_to_prefix(self, tmp_path):
        plain = self.write_skewed_fasta(tmp_path / "skewed.fasta")
        gz = tmp_path / "skewed.fasta.gz"
        with open(plain, "rb") as src, gzip.open(gz, "wb") as dst:
            dst.write(src.read())
        r = ss.fasta_stats(gz, Compression.GZIP, max_bases=100_000)
        # Gzip cannot seek cheaply, so the prefix read stands -- but it must be
        # labelled as such rather than claiming to span the file.
        assert r["stats_sampling"] == "prefix"
        assert "gc_content_percent" in r

    def test_tiny_file_does_not_overcount_via_degenerate_stride(self, tmp_path):
        """A file smaller than FASTA_SAMPLE_BLOCKS must not stride.

        `stride = file_size // FASTA_SAMPLE_BLOCKS` truncates to 0 for any
        file under 100 bytes, which would collapse every seek onto offset 0
        and re-read the same handful of bytes ~100 times over. `max_bases` is
        set below the file's own base count so the file still exceeds the
        sampling budget -- the condition that used to force the strided path.
        """
        p = tmp_path / "tiny.fasta"
        p.write_text(">c1\nGCAT\n")  # 4 bases, well under FASTA_SAMPLE_BLOCKS
        r = ss.fasta_stats(p, Compression.NONE, max_bases=2)
        # No ~100x overcounting: sampled bases can't exceed what's in the file.
        assert r["stats_sampled_bases"] <= 4
        assert r["stats_sampling"] in ("prefix", "complete")
