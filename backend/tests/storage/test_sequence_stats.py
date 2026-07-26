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
