from pathlib import Path

import pytest

from app.pipelines import gci_runner


def test_command_routes_hifi_bam_to_hifi_flag():
    cmd = gci_runner.build_gci_command(
        gci_path="/usr/local/bin/gci",
        assembly=Path("/work/assembly.fasta"),
        hifi_bam=Path("/work/hifi.bam"),
        nano_bam=None,
        out_dir=Path("/work/out"),
        prefix="gci",
        threads=8,
        map_qual=30,
        plot=False,
    )
    assert "--hifi" in cmd
    assert "/work/hifi.bam" in cmd
    assert "--nano" not in cmd


def test_command_routes_both_slots_when_both_present():
    cmd = gci_runner.build_gci_command(
        gci_path="/usr/local/bin/gci",
        assembly=Path("/work/assembly.fasta"),
        hifi_bam=Path("/work/hifi.bam"),
        nano_bam=Path("/work/nano.bam"),
        out_dir=Path("/work/out"),
        prefix="gci",
        threads=8,
        map_qual=30,
        plot=False,
    )
    assert "--hifi" in cmd and "--nano" in cmd


def test_command_records_map_qual():
    """-mq is not a tuning knob that can be left implicit: upstream is
    explicit that lowering it pulls in multi-mapping reads from repetitive
    regions, so two runs at different -mq are not comparable.
    """
    cmd = gci_runner.build_gci_command(
        gci_path="/usr/local/bin/gci",
        assembly=Path("/work/assembly.fasta"),
        hifi_bam=Path("/work/hifi.bam"),
        nano_bam=None,
        out_dir=Path("/work/out"),
        prefix="gci",
        threads=8,
        map_qual=50,
        plot=False,
    )
    assert "-mq" in cmd
    assert "50" in cmd


def test_command_omits_plot_flag_when_disabled():
    """One image per chromosome means a fragmented assembly produces
    hundreds of files. Plotting is gated, not default-on.
    """
    cmd = gci_runner.build_gci_command(
        gci_path="/usr/local/bin/gci",
        assembly=Path("/work/assembly.fasta"),
        hifi_bam=Path("/work/hifi.bam"),
        nano_bam=None,
        out_dir=Path("/work/out"),
        prefix="gci",
        threads=8,
        map_qual=30,
        plot=False,
    )
    assert "-p" not in cmd


def test_command_uses_fixed_input_names():
    """The assembly is linked under a fixed name so an object name never
    reaches an output path -- QUAST's stored-XSS lesson, applied here
    before the bug exists.
    """
    cmd = gci_runner.build_gci_command(
        gci_path="/usr/local/bin/gci",
        assembly=Path("/work/assembly.fasta"),
        hifi_bam=Path("/work/hifi.bam"),
        nano_bam=None,
        out_dir=Path("/work/out"),
        prefix="gci",
        threads=8,
        map_qual=30,
        plot=False,
    )
    assert not any(";" in part or "<" in part for part in cmd)


def test_parse_gci_returns_typed_facts():
    """Counts and N50s are ints, scores are floats. QUAST's slice shipped a
    float where an int was meant, invisibly, because 2 == 2.0 and every
    assertion was equality-only.
    """
    text = (
        "Chromosome\tExpected N50\tObserved N50\tExpected number of contigs\t"
        "Observed number of contigs\tGenome Continuity Index\n"
        "Genome\t12157105\t8452103\t16\t23\t41.8259\n"
    )
    parsed = gci_runner.parse_gci(text)
    assert parsed["assembly_continuity_gci"] == pytest.approx(41.8259)
    assert isinstance(parsed["assembly_continuity_gci"], float)
    assert isinstance(parsed["assembly_continuity_expected_n50"], int)
    assert isinstance(parsed["assembly_continuity_observed_contigs"], int)


def test_parse_gci_raises_on_empty():
    with pytest.raises(ValueError):
        gci_runner.parse_gci("")
