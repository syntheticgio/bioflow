from pathlib import Path

import pytest

from app.pipelines import merqury_runner


def test_meryl_count_command_includes_k_and_output():
    cmd = merqury_runner.build_meryl_count_command(
        meryl_path="/opt/meryl/bin/meryl",
        k=21,
        reads=[Path("/work/reads.fastq.gz")],
        output=Path("/work/reads.meryl"),
        threads=4,
    )
    assert cmd[0] == "/opt/meryl/bin/meryl"
    assert "count" in cmd
    assert "k=21" in cmd
    assert "output" in cmd
    assert "/work/reads.meryl" in cmd
    assert "/work/reads.fastq.gz" in cmd
    assert "threads=4" in cmd


def test_meryl_count_command_accepts_multiple_read_files():
    """Paired-end reads are two files and both k-mer sets belong in one
    database -- the QV denominator is the whole read set, not one mate.
    """
    cmd = merqury_runner.build_meryl_count_command(
        meryl_path="/opt/meryl/bin/meryl",
        k=21,
        reads=[Path("/work/r1.fastq.gz"), Path("/work/r2.fastq.gz")],
        output=Path("/work/reads.meryl"),
        threads=4,
    )
    assert "/work/r1.fastq.gz" in cmd
    assert "/work/r2.fastq.gz" in cmd


def test_merqury_command_uses_fixed_names():
    """The assembly is passed under a fixed link name, never its own.
    merqury.sh derives every output filename from the input basename, so a
    hostile or merely awkward object name would otherwise reach an output
    path. Same lesson QUAST's slice learned as a stored XSS.
    """
    cmd = merqury_runner.build_merqury_command(
        merqury_path="/usr/local/bin/merqury",
        read_db=Path("/work/reads.meryl"),
        assembly=Path("/work/assembly.fasta"),
        out_prefix="qv",
    )
    assert cmd == [
        "/usr/local/bin/merqury",
        "/work/reads.meryl",
        "/work/assembly.fasta",
        "qv",
    ]


def test_parse_qv_reads_the_assembly_row():
    """Merqury's .qv is tab-separated with no header:
    <asm>  <asm-only kmers>  <total kmers>  <QV>  <error rate>
    """
    text = "assembly\t14903\t12157105\t35.4728\t0.000283749\n"
    parsed = merqury_runner.parse_qv(text)
    assert parsed["assembly_qv"] == pytest.approx(35.4728)
    assert parsed["assembly_qv_error_rate"] == pytest.approx(0.000283749)


def test_parse_qv_values_are_floats_not_ints():
    """QUAST's slice shipped a float where an int was meant, invisibly,
    because 2 == 2.0 and every assertion was equality-only. Assert type.
    """
    text = "assembly\t0\t12157105\t60\t0\n"
    parsed = merqury_runner.parse_qv(text)
    assert isinstance(parsed["assembly_qv"], float)
    assert isinstance(parsed["assembly_qv_error_rate"], float)


def test_parse_completeness_reads_the_percentage():
    """completeness.stats is tab-separated:
    <asm>  <solid kmers found>  <total solid kmers>  <completeness %>
    """
    text = "assembly\tall\t11842013\t12157105\t97.4082\n"
    parsed = merqury_runner.parse_completeness(text)
    assert parsed["assembly_qv_completeness_pct"] == pytest.approx(97.4082)
    assert isinstance(parsed["assembly_qv_completeness_pct"], float)


def test_parse_qv_raises_on_empty_report():
    with pytest.raises(ValueError):
        merqury_runner.parse_qv("")
