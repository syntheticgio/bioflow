"""Unit tests for the StringTie runner.

The GTF fixtures here are real StringTie 2.2.1 output, produced against a
synthetic BAM and a two-exon reference GTF on 2026-08-20, not hand-written
from memory. The attribute names this module keys on -- `reference_id` present
on a transcript the reference already had, absent on one StringTie proposed --
are the single fact `parse_gtf` depends on, so they are worth pinning to real
output rather than recall.
"""

from pathlib import Path

from app.pipelines import stringtie_runner

# A transcript StringTie matched to the reference: carries reference_id.
MATCHED_GTF = """\
# stringtie in.bam -G ref.gtf -o out.gtf -p 2
# StringTie version 2.2.1
chr1\tStringTie\ttranscript\t101\t500\t1000\t+\t.\tgene_id "STRG.1"; transcript_id "STRG.1.1"; reference_id "T1"; ref_gene_id "G1"; cov "30.000000"; FPKM "3333333.500000"; TPM "1000000.000000";
chr1\tStringTie\texon\t101\t200\t1000\t+\t.\tgene_id "STRG.1"; transcript_id "STRG.1.1"; exon_number "1"; reference_id "T1"; ref_gene_id "G1"; cov "30.000000";
chr1\tStringTie\texon\t401\t500\t1000\t+\t.\tgene_id "STRG.1"; transcript_id "STRG.1.1"; exon_number "2"; reference_id "T1"; ref_gene_id "G1"; cov "30.000000";
"""

# A transcript StringTie proposed: no reference_id, no ref_gene_id.
NOVEL_GTF = """\
# stringtie in.bam -G ref.gtf -o out.gtf -p 2
# StringTie version 2.2.1
chr1\tStringTie\ttranscript\t1201\t1700\t1000\t+\t.\tgene_id "STRG.1"; transcript_id "STRG.1.1"; cov "60.000000"; FPKM "5000000.000000"; TPM "1000000.000000";
chr1\tStringTie\texon\t1201\t1300\t1000\t+\t.\tgene_id "STRG.1"; transcript_id "STRG.1.1"; exon_number "1"; cov "60.000000";
chr1\tStringTie\texon\t1601\t1700\t1000\t+\t.\tgene_id "STRG.1"; transcript_id "STRG.1.1"; exon_number "2"; cov "60.000000";
"""


def test_assemble_command_builds_reference_guided_argv():
    argv = stringtie_runner.assemble_command(
        bam=Path("/w/in.bam"),
        annotation=Path("/w/ref.gtf"),
        out_gtf=Path("/w/out.gtf"),
        stringtie_path="/usr/bin/stringtie",
        threads=4,
    )
    assert argv == [
        "/usr/bin/stringtie",
        "/w/in.bam",
        "-G",
        "/w/ref.gtf",
        "-o",
        "/w/out.gtf",
        "-p",
        "4",
    ]


def test_assemble_command_defaults_to_one_thread():
    argv = stringtie_runner.assemble_command(
        bam=Path("/w/in.bam"),
        annotation=Path("/w/ref.gtf"),
        out_gtf=Path("/w/out.gtf"),
        stringtie_path="stringtie",
    )
    assert argv[-2:] == ["-p", "1"]


def test_parse_gtf_counts_a_matched_transcript_as_not_novel():
    facts = stringtie_runner.parse_gtf(MATCHED_GTF)
    assert facts["transcript_count"] == 1
    assert facts["novel_transcript_count"] == 0
    assert facts["gene_count"] == 1


def test_parse_gtf_counts_a_transcript_without_reference_id_as_novel():
    facts = stringtie_runner.parse_gtf(NOVEL_GTF)
    assert facts["transcript_count"] == 1
    assert facts["novel_transcript_count"] == 1


def test_parse_gtf_ignores_exon_lines_when_counting_transcripts():
    # Both fixtures carry two exon lines per transcript. A parser keying on
    # the attribute rather than the feature column would count three.
    facts = stringtie_runner.parse_gtf(MATCHED_GTF + NOVEL_GTF)
    assert facts["transcript_count"] == 2
    assert facts["novel_transcript_count"] == 1


def test_parse_gtf_on_empty_output_reports_zeroes_rather_than_raising():
    # StringTie exits zero on a BAM with no assemblable coverage, writing a
    # header-only GTF. That is an empty result, not a failure.
    facts = stringtie_runner.parse_gtf("# StringTie version 2.2.1\n")
    assert facts == {
        "transcript_count": 0,
        "novel_transcript_count": 0,
        "gene_count": 0,
    }
