"""Header parsing against real BAM/VCF/FASTQ/FASTA files.

Fixtures are generated with pysam at test time rather than committed: a real
BAM is a compressed binary that would be opaque in review, and generating it
proves the parser works against files htslib itself produced.
"""

import gzip
import threading

import pytest

from app.errors import JobCancelled
from app.models import Compression, FormatKind
from app.storage import parsers

pysam = pytest.importorskip("pysam")


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture
def bam(tmp_path):
    path = tmp_path / "sample.bam"
    header = {
        "HD": {"VN": "1.6", "SO": "coordinate"},
        "SQ": [
            {"SN": "chr1", "LN": 248956422},
            {"SN": "chr2", "LN": 242193529},
        ],
        "RG": [{"ID": "rg1", "SM": "PatientA", "PL": "ILLUMINA"}],
        "PG": [{"ID": "bwa", "PN": "bwa", "VN": "0.7.17"}],
    }
    with pysam.AlignmentFile(str(path), "wb", header=header) as out:
        for i in range(10):
            a = pysam.AlignedSegment()
            a.query_name = f"read{i}"
            a.query_sequence = "ACGT" * 25  # 100 bp
            a.flag = 99  # paired, proper pair
            a.reference_id = 0
            a.reference_start = 1000 + i * 100
            a.mapping_quality = 60
            a.cigar = [(0, 100)]
            a.next_reference_id = 0
            a.next_reference_start = 1200 + i * 100
            a.template_length = 300
            a.query_qualities = pysam.qualitystring_to_array("I" * 100)
            a.set_tag("RG", "rg1")
            out.write(a)
    return path


@pytest.fixture
def vcf(tmp_path):
    path = tmp_path / "calls.vcf"
    path.write_text(
        "##fileformat=VCFv4.2\n"
        "##contig=<ID=chr1,length=248956422>\n"
        "##contig=<ID=chr2,length=242193529>\n"
        '##INFO=<ID=DP,Number=1,Type=Integer,Description="Depth">\n'
        '##INFO=<ID=AF,Number=A,Type=Float,Description="Allele Frequency">\n'
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
        '##FILTER=<ID=LowQual,Description="Low quality">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSampleA\tSampleB\n"
        "chr1\t100\t.\tA\tG\t50\tPASS\tDP=30\tGT\t0/1\t1/1\n"
        "chr1\t200\t.\tAT\tA\t60\tPASS\tDP=25\tGT\t0/1\t0/0\n"
        "chr2\t300\t.\tC\tCTT\t70\tPASS\tDP=40\tGT\t1/1\t0/1\n"
    )
    return path


@pytest.fixture
def fastq(tmp_path):
    path = tmp_path / "reads.fastq"
    with open(path, "w") as f:
        for i in range(500):
            f.write(f"@READ_{i} instrument:1:flowcell\n")
            f.write("ACGT" * 25 + "\n")
            f.write("+\n")
            f.write("I" * 100 + "\n")
    return path


@pytest.fixture
def fasta(tmp_path):
    path = tmp_path / "ref.fasta"
    path.write_text(
        ">chr1 first contig\n" + "ACGT" * 15 + "\n"
        ">chr2 second\n" + "TTTT" * 10 + "\n"
        ">chr3\n" + "GGGG" * 5 + "\n"
    )
    return path


# --- Alignment --------------------------------------------------------------


class TestBam:
    def test_extracts_reference_contigs(self, bam):
        f = parsers.parse(bam, FormatKind.BAM, Compression.BGZF)
        assert f["reference_count"] == 2
        assert f["reference_names"] == ["chr1", "chr2"]
        assert f["reference_lengths"]["chr1"] == 248956422
        assert f["reference_total_length"] == 248956422 + 242193529

    def test_extracts_sample_and_platform(self, bam):
        f = parsers.parse(bam, FormatKind.BAM, Compression.BGZF)
        assert f["sample_names"] == ["PatientA"]
        assert f["platforms"] == ["ILLUMINA"]
        assert f["read_group_count"] == 1

    def test_extracts_sort_order(self, bam):
        """Whether a BAM is coordinate-sorted decides what can run on it."""
        f = parsers.parse(bam, FormatKind.BAM, Compression.BGZF)
        assert f["sort_order"] == "coordinate"
        assert f["sam_version"] == "1.6"

    def test_records_program_chain(self, bam):
        f = parsers.parse(bam, FormatKind.BAM, Compression.BGZF)
        assert "bwa" in f["program_chain"]

    def test_samples_read_length_and_pairing(self, bam):
        f = parsers.parse(bam, FormatKind.BAM, Compression.BGZF)
        assert f["read_length"] == 100
        assert f["paired"] is True

    def test_reports_index_absence(self, bam):
        f = parsers.parse(bam, FormatKind.BAM, Compression.BGZF)
        assert f["has_index"] is False

    def test_detects_a_present_index(self, bam):
        pysam.index(str(bam))
        f = parsers.parse(bam, FormatKind.BAM, Compression.BGZF)
        assert f["has_index"] is True

    def test_header_only_bam_does_not_crash(self, tmp_path):
        path = tmp_path / "empty.bam"
        with pysam.AlignmentFile(
            str(path), "wb", header={"HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 100}]}
        ):
            pass
        f = parsers.parse(path, FormatKind.BAM, Compression.BGZF)
        assert f["reference_count"] == 1
        assert "read_length" not in f  # nothing to sample, reported honestly


class TestSam:
    def test_parses_plain_text_sam(self, tmp_path, bam):
        sam = tmp_path / "aln.sam"
        with pysam.AlignmentFile(str(bam), "rb") as inp:
            with pysam.AlignmentFile(str(sam), "w", template=inp) as out:
                for rec in inp:
                    out.write(rec)
        f = parsers.parse(sam, FormatKind.SAM, Compression.NONE)
        assert f["reference_count"] == 2
        assert f["sample_names"] == ["PatientA"]


# --- Variants ---------------------------------------------------------------


class TestVcf:
    def test_extracts_samples(self, vcf):
        f = parsers.parse(vcf, FormatKind.VCF, Compression.NONE)
        assert f["sample_count"] == 2
        assert f["sample_names"] == ["SampleA", "SampleB"]

    def test_extracts_version_and_contigs(self, vcf):
        f = parsers.parse(vcf, FormatKind.VCF, Compression.NONE)
        assert "4.2" in f["vcf_version"]
        assert f["reference_count"] == 2
        assert f["reference_names"] == ["chr1", "chr2"]

    def test_extracts_info_and_format_fields(self, vcf):
        f = parsers.parse(vcf, FormatKind.VCF, Compression.NONE)
        assert "DP" in f["info_fields"]
        assert "GT" in f["format_fields"]
        assert "LowQual" in f["filters"]

    def test_classifies_variant_types(self, vcf):
        """SNV, insertion and deletion are all present in the fixture."""
        f = parsers.parse(vcf, FormatKind.VCF, Compression.NONE)
        assert set(f["variant_types_sampled"]) == {"SNV", "insertion", "deletion"}

    def test_small_file_record_count_is_exact(self, vcf):
        f = parsers.parse(vcf, FormatKind.VCF, Compression.NONE)
        assert f["record_count"] == 3
        assert f["record_count_exact"] is True

    def test_bgzf_vcf(self, tmp_path, vcf):
        gz = tmp_path / "calls.vcf.gz"
        pysam.tabix_compress(str(vcf), str(gz), force=True)
        f = parsers.parse(gz, FormatKind.VCF, Compression.BGZF)
        assert f["sample_names"] == ["SampleA", "SampleB"]


# --- Sequences --------------------------------------------------------------


class TestFastq:
    def test_extracts_read_length(self, fastq):
        f = parsers.parse(fastq, FormatKind.FASTQ, Compression.NONE)
        assert f["read_length"] == 100
        assert f["sampled_records"] == 500

    def test_read_count_is_estimated_and_labelled_inexact(self, fastq):
        """The estimate must never be presented as a fact -- someone will cite it."""
        f = parsers.parse(fastq, FormatKind.FASTQ, Compression.NONE)
        assert f["read_count_exact"] is False
        # 500 real records; the estimate extrapolates from a uniform sample.
        assert 450 <= f["read_count_estimate"] <= 550

    def test_captures_first_read_ids(self, fastq):
        f = parsers.parse(fastq, FormatKind.FASTQ, Compression.NONE)
        assert f["first_read_ids"][0].startswith("READ_0")

    def test_gzipped_fastq_is_estimated_with_a_note(self, tmp_path, fastq):
        gz = tmp_path / "reads.fastq.gz"
        gz.write_bytes(gzip.compress(fastq.read_bytes()))
        f = parsers.parse(gz, FormatKind.FASTQ, Compression.GZIP)
        assert f["read_length"] == 100
        assert f["read_count_exact"] is False
        assert "approximate" in f["estimate_note"].lower()
        # Compressed extrapolation is rough, but must land in the right order
        # of magnitude rather than being wildly wrong.
        assert 250 <= f["read_count_estimate"] <= 1000

    @pytest.mark.parametrize(
        "filename,expected",
        [
            ("Sample_R1.fastq", "R1"),
            ("Sample_R2.fastq", "R2"),
            ("sample_1.fastq", "R1"),
            ("unpaired.fastq", None),
        ],
    )
    def test_infers_pair_hint_from_filename(self, tmp_path, fastq, filename, expected):
        target = tmp_path / filename
        target.write_bytes(fastq.read_bytes())
        f = parsers.parse(target, FormatKind.FASTQ, Compression.NONE)
        assert f.get("paired_hint") == expected

    @pytest.mark.parametrize("filename,expected", [("Sample_R1.fastq", "R1"), ("s_R2.fq", "R2")])
    def test_pair_hint_uses_display_name_not_blob_path(
        self, tmp_path, fastq, filename, expected
    ):
        """Managed blobs are stored under their SHA-256, so the on-disk name is
        a hex digest. Without the display name threaded through, mate detection
        silently never fires for uploaded files."""
        blob_like = tmp_path / ("a" * 64)
        blob_like.write_bytes(fastq.read_bytes())

        without = parsers.parse(blob_like, FormatKind.FASTQ, Compression.NONE)
        assert "paired_hint" not in without

        with_name = parsers.parse(
            blob_like, FormatKind.FASTQ, Compression.NONE, display_name=filename
        )
        assert with_name["paired_hint"] == expected

    def test_mismatched_quality_length_is_flagged(self, tmp_path):
        path = tmp_path / "bad.fastq"
        path.write_text("@r1\nACGTACGT\n+\nII\n")  # 8 bases, 2 quality scores
        f = parsers.parse(path, FormatKind.FASTQ, Compression.NONE)
        assert "disagree" in f["parse_warning"]

    def test_malformed_structure_is_flagged_not_raised(self, tmp_path):
        path = tmp_path / "bad.fastq"
        path.write_text("@r1\nACGT\nNOT_A_PLUS\nIIII\n")
        f = parsers.parse(path, FormatKind.FASTQ, Compression.NONE)
        assert "parse_warning" in f

    def test_empty_file_returns_no_facts(self, tmp_path):
        path = tmp_path / "empty.fastq"
        path.write_text("")
        assert parsers.parse(path, FormatKind.FASTQ, Compression.NONE) == {}


class TestFasta:
    def test_counts_sequences_exactly_for_small_files(self, fasta):
        f = parsers.parse(fasta, FormatKind.FASTA, Compression.NONE)
        assert f["sequence_count"] == 3
        assert f["sequence_count_exact"] is True
        assert f["sequence_names"] == ["chr1", "chr2", "chr3"]

    def test_totals_bases(self, fasta):
        f = parsers.parse(fasta, FormatKind.FASTA, Compression.NONE)
        assert f["total_bases"] == 60 + 40 + 20

    def test_gzipped_fasta(self, tmp_path, fasta):
        gz = tmp_path / "ref.fa.gz"
        gz.write_bytes(gzip.compress(fasta.read_bytes()))
        f = parsers.parse(gz, FormatKind.FASTA, Compression.GZIP)
        assert f["sequence_count"] == 3


class TestTabular:
    def test_bed_columns_and_contigs(self, tmp_path):
        path = tmp_path / "regions.bed"
        path.write_text(
            "track name=test\n"
            "chr1\t100\t200\tfeat1\n"
            "chr1\t300\t400\tfeat2\n"
            "chr2\t500\t600\tfeat3\n"
        )
        f = parsers.parse(path, FormatKind.BED, Compression.NONE)
        assert f["sampled_records"] == 3
        assert f["column_counts"] == [4]
        assert f["reference_names"] == ["chr1", "chr2"]
        assert f["header_lines"] == 1

    def test_inconsistent_columns_are_flagged(self, tmp_path):
        path = tmp_path / "ragged.bed"
        path.write_text("chr1\t100\t200\nchr1\t300\t400\textra\n")
        f = parsers.parse(path, FormatKind.BED, Compression.NONE)
        assert "inconsistent" in f["parse_warning"]


# --- Robustness -------------------------------------------------------------


class TestRobustness:
    def test_corrupt_bam_reports_an_error_rather_than_raising(self, tmp_path):
        """A bad file must mark the object, not crash the worker."""
        path = tmp_path / "corrupt.bam"
        path.write_bytes(b"\x1f\x8b\x08\x04" + b"\x00" * 200)
        f = parsers.parse(path, FormatKind.BAM, Compression.BGZF)
        assert "parse_error" in f

    def test_truncated_vcf_does_not_raise(self, tmp_path):
        path = tmp_path / "truncated.vcf"
        path.write_text("##fileformat=VCFv4.2\n#CHROM\tPOS")
        result = parsers.parse(path, FormatKind.VCF, Compression.NONE)
        assert isinstance(result, dict)

    def test_unknown_format_returns_empty(self, tmp_path):
        path = tmp_path / "mystery.dat"
        path.write_bytes(b"\x00\x01\x02")
        assert parsers.parse(path, FormatKind.UNKNOWN, Compression.NONE) == {}

    def test_cancellation_propagates(self, tmp_path):
        """Cancellation must not be swallowed by the catch-all error handling."""
        path = tmp_path / "reads.fastq"
        with open(path, "w") as f:
            for i in range(300):
                f.write(f"@r{i}\nACGT\n+\nIIII\n")

        event = threading.Event()
        event.set()
        with pytest.raises(JobCancelled):
            parsers.parse(
                path, FormatKind.FASTQ, Compression.NONE, cancel_event=event
            )


class TestContigTruncation:
    def test_large_contig_lists_are_bounded(self, tmp_path):
        """Scaffold-level assemblies have hundreds of thousands of contigs;
        storing them all would bloat every object document."""
        sq = [{"SN": f"scaffold_{i}", "LN": 1000 + i} for i in range(200)]
        path = tmp_path / "many.bam"
        with pysam.AlignmentFile(
            str(path), "wb", header={"HD": {"VN": "1.6"}, "SQ": sq}
        ):
            pass

        f = parsers.parse(path, FormatKind.BAM, Compression.BGZF)
        assert f["reference_count"] == 200  # true count preserved
        assert len(f["reference_names"]) == parsers.MAX_STORED_CONTIGS
        assert f["reference_names_truncated"] is True


class TestFastaSequenceLengths:
    """Per-sequence lengths, and the longest/shortest across an assembly."""

    def test_lengths_and_extremes(self, tmp_path):
        p = tmp_path / "ref.fasta"
        p.write_text(">c1\nACGT\n>c2\nACGTACGTAC\n>c3\nAC\n")
        facts = parsers.parse(p, FormatKind.FASTA, Compression.NONE)
        assert facts["sequence_lengths"] == {"c1": 4, "c2": 10, "c3": 2}
        assert facts["sequence_longest"] == {"name": "c2", "length": 10}
        assert facts["sequence_shortest"] == {"name": "c3", "length": 2}

    def test_wrapped_records_sum_across_lines(self, tmp_path):
        p = tmp_path / "wrapped.fasta"
        # 3 lines x 60 bases: a real FASTA wraps, and a per-line count would
        # report 60 instead of 180.
        p.write_text(">chr1\n" + ("A" * 60 + "\n") * 3 + ">chr2\nAC\n")
        facts = parsers.parse(p, FormatKind.FASTA, Compression.NONE)
        assert facts["sequence_lengths"]["chr1"] == 180
        assert facts["sequence_longest"] == {"name": "chr1", "length": 180}

    def test_extremes_come_from_beyond_the_stored_window(self, tmp_path):
        """The stored dict is capped, but longest/shortest are not.

        Bounding the extremes to the first MAX_STORED_CONTIGS would report the
        wrong longest contig for any assembly with more sequences than that --
        which is most of them.
        """
        n = parsers.MAX_STORED_CONTIGS + 10
        with open(tmp_path / "many.fasta", "w") as f:
            for i in range(n):
                # Records grow, so both extremes sit outside the first 50:
                # the shortest is record 0... so make record n-1 longest and
                # deliberately place the shortest late as well.
                f.write(f">c{i}\n" + "A" * (100 + i) + "\n")
            f.write(">tiny\nA\n")
        facts = parsers.parse(tmp_path / "many.fasta", FormatKind.FASTA, Compression.NONE)
        assert len(facts["sequence_lengths"]) == parsers.MAX_STORED_CONTIGS
        assert facts["sequence_longest"] == {"name": f"c{n - 1}", "length": 100 + n - 1}
        assert facts["sequence_shortest"] == {"name": "tiny", "length": 1}

    def test_single_sequence_is_both_extremes(self, tmp_path):
        p = tmp_path / "one.fasta"
        p.write_text(">only\nACGTACGT\n")
        facts = parsers.parse(p, FormatKind.FASTA, Compression.NONE)
        assert facts["sequence_longest"] == {"name": "only", "length": 8}
        assert facts["sequence_shortest"] == {"name": "only", "length": 8}

    def test_complete_parse_is_not_flagged_partial(self, tmp_path):
        p = tmp_path / "ref.fasta"
        p.write_text(">c1\nACGT\n")
        facts = parsers.parse(p, FormatKind.FASTA, Compression.NONE)
        assert "sequence_lengths_partial" not in facts
