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


def test_per_chromosome_rows_are_not_mistaken_for_the_genome_row():
    """GCI emits one row per chromosome plus a whole-assembly `Genome`
    aggregate row. Per-chromosome rows appearing before `Genome` must not
    be picked as the result -- only the `Genome` row's values are facts.
    """
    text = (
        "Chromosome\tExpected N50\tObserved N50\tExpected number of contigs\t"
        "Observed number of contigs\tGenome Continuity Index\n"
        "chr1\t1000000\t500000\t1\t3\t10.0\n"
        "chr2\t2000000\t900000\t1\t2\t20.0\n"
        "Genome\t12157105\t8452103\t16\t23\t41.8259\n"
    )
    parsed = gci_runner.parse_gci(text)
    assert parsed["assembly_continuity_expected_n50"] == 12157105
    assert parsed["assembly_continuity_observed_n50"] == 8452103
    assert parsed["assembly_continuity_expected_contigs"] == 16
    assert parsed["assembly_continuity_observed_contigs"] == 23
    assert parsed["assembly_continuity_gci"] == pytest.approx(41.8259)


def test_parse_gci_raises_when_no_genome_row_present():
    """Only per-chromosome rows, no `Genome` aggregate -- this must raise
    rather than silently returning a per-chromosome row's values.
    """
    text = (
        "Chromosome\tExpected N50\tObserved N50\tExpected number of contigs\t"
        "Observed number of contigs\tGenome Continuity Index\n"
        "chr1\t1000000\t500000\t1\t3\t10.0\n"
    )
    with pytest.raises(ValueError):
        gci_runner.parse_gci(text)


def test_parse_gci_raises_with_context_on_non_numeric_field():
    """A bad numeric field in the Genome row must raise a clear, contextual
    ValueError -- not a bare Python float() ValueError with no context.
    """
    text = (
        "Chromosome\tExpected N50\tObserved N50\tExpected number of contigs\t"
        "Observed number of contigs\tGenome Continuity Index\n"
        "Genome\t12157105\tNA\t16\t23\t41.8259\n"
    )
    with pytest.raises(ValueError, match="could not parse GCI Genome row"):
        gci_runner.parse_gci(text)


def test_parse_gci_matches_real_zenodo_example_output():
    """Fixture captured verbatim from a real GCI v1.0 run against upstream's
    own example dataset (Zenodo record 12748594, `example.tar.gz`):

        GCI.py -r MH63.fasta \
            --hifi MH63_winnowmap_hifi.subsample.bam MH63.minimap2_hifi.subsample.paf \
            -d out -o MH63

    Byte-identical to the `MH63.gci` shipped in the tarball. Confirms the
    parser survives the leading `"HiFi:"` chemistry label line and the
    trailing dash separator that upstream's prose description omits.
    """
    text = (
        "HiFi:\n"
        "Chromosome\tTheoretical maximum N50\tCurated N50\t"
        "Theoretical minimum contigs number\tCurated contigs number\tGCI score\n"
        "Chr01_MH63\t45027022\t266013\t1\t65\t0.1406\n"
        "Chr02_MH63\t37301368\t282516\t1\t74\t0.1748\n"
        "Chr03_MH63\t39893253\t216310\t1\t64\t0.1295\n"
        "Chr04_MH63\t37319239\t266049\t1\t75\t0.164\n"
        "Chr05_MH63\t31307418\t261452\t1\t62\t0.2007\n"
        "Chr06_MH63\t31921180\t292575\t1\t77\t0.2094\n"
        "Chr07_MH63\t30877072\t306329\t1\t76\t0.2273\n"
        "Chr08_MH63\t30492302\t282597\t1\t68\t0.2179\n"
        "Chr09_MH63\t24892599\t208687\t1\t75\t0.1928\n"
        "Chr10_MH63\t25690566\t284384\t1\t69\t0.2591\n"
        "Chr11_MH63\t34100580\t295041\t1\t72\t0.2008\n"
        "Chr12_MH63\t26942889\t237900\t1\t73\t0.2042\n"
        "Genome\t31921180\t259735\t12\t850\t0.1896\n"
        "----------------------------------------------------------------"
        "----------------------------------------------------------------"
        "------------------------\n"
    )
    parsed = gci_runner.parse_gci(text)
    assert parsed == {
        "assembly_continuity_expected_n50": 31921180,
        "assembly_continuity_observed_n50": 259735,
        "assembly_continuity_expected_contigs": 12,
        "assembly_continuity_observed_contigs": 850,
        "assembly_continuity_gci": pytest.approx(0.1896),
    }
    assert isinstance(parsed["assembly_continuity_expected_n50"], int)
    assert isinstance(parsed["assembly_continuity_gci"], float)


def test_build_gci_command_raises_when_no_bam_at_all():
    """Reaching the command builder with neither BAM slot set is a caller
    bug -- the same invariant `craq_runner.build_craq_command` enforces for
    its own ngs_bam/sms_bam pair.
    """
    with pytest.raises(ValueError):
        gci_runner.build_gci_command(
            gci_path="/usr/local/bin/gci",
            assembly=Path("/work/assembly.fasta"),
            hifi_bam=None,
            nano_bam=None,
            out_dir=Path("/work/out"),
            prefix="gci",
            threads=8,
            map_qual=30,
            plot=False,
        )
