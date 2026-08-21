"""Pure command-building and quality-report parsing for CheckM2.

The table shapes here are the ones CheckM2 1.1.0 actually writes, read from
`predictQuality.py` in a real bioconda install on 2026-08-21 -- including the
mode-dependent column set, which is the trap this parser exists to survive.
"""

import pytest

from app.pipelines import checkm2_runner
from app.pipelines.checkm2_runner import (
    BinQuality,
    bin_quality_facts,
    build_predict_command,
    parse_quality_report,
    quality_tier,
)

# The default `checkm2 predict` table: `Completeness` and `Contamination`.
DEFAULT_TABLE = (
    "Name\tCompleteness\tContamination\tCompleteness_Model_Used\t"
    "Coding_Density\tContig_N50\tGenome_Size\tGC_Content\tTotal_Contigs\t"
    "Additional_Notes\n"
    "bin.1\t98.55\t1.20\tNeural Network (Specific Model)\t0.887\t45210\t"
    "4210233\t0.62\t83\tNone\n"
    "bin.2\t45.10\t8.75\tGradient Boost (General Model)\t0.901\t12044\t"
    "1980221\t0.41\t210\tNone\n"
)

# `--allmodels`: no plain `Completeness` column at all.
ALLMODELS_TABLE = (
    "Name\tCompleteness_General\tContamination\tCompleteness_Specific\t"
    "Completeness_Model_Used\n"
    "bin.1\t91.40\t2.10\t94.75\tNeural Network (Specific Model)\n"
)


def test_build_predict_command_shape():
    cmd = build_predict_command(
        checkm2_path="/usr/local/bin/checkm2",
        bins_dir="/work/bins",
        output_dir="/work/out",
        database_path="/db/uniref100.KO.1.dmnd",
        threads=8,
    )
    assert cmd[:2] == ["/usr/local/bin/checkm2", "predict"]
    assert "--input" in cmd and "/work/bins" in cmd
    assert "--output-directory" in cmd and "/work/out" in cmd
    # The explicit per-run path, not the CHECKM2DB environment variable (S-5).
    assert "--database_path" in cmd
    assert "/db/uniref100.KO.1.dmnd" in cmd
    assert "--force" in cmd
    assert cmd[cmd.index("--threads") + 1] == "8"


def test_extension_defaults_to_fa_not_fna():
    """MetaBAT2 writes `.fa`; CheckM2's own default is `.fna`.

    Leaving CheckM2's default in place makes it find zero bins and exit
    SUCCESSFULLY with an empty table -- a silent no-op, which is why the
    extension is asserted rather than left implicit.
    """
    cmd = build_predict_command(
        checkm2_path="checkm2",
        bins_dir="/work/bins",
        output_dir="/work/out",
        database_path="/db/x.dmnd",
    )
    assert cmd[cmd.index("--extension") + 1] == "fa"


def test_lowmem_is_opt_in():
    base = dict(
        checkm2_path="checkm2",
        bins_dir="/b",
        output_dir="/o",
        database_path="/db/x.dmnd",
    )
    assert "--lowmem" not in build_predict_command(**base)
    assert "--lowmem" in build_predict_command(**base, lowmem=True)


def test_parse_default_table():
    rows = parse_quality_report(DEFAULT_TABLE)
    assert [r.name for r in rows] == ["bin.1", "bin.2"]
    assert rows[0].completeness == pytest.approx(98.55)
    assert rows[0].contamination == pytest.approx(1.20)
    assert rows[0].genome_size == 4210233
    assert rows[0].contig_n50 == 45210
    assert rows[0].total_contigs == 83


def test_parse_allmodels_table_falls_back_to_specific():
    """An --allmodels table has no `Completeness` column.

    A parser keyed only on `Completeness` reads this as having no scores at
    all rather than failing loudly -- so the fallback is asserted.
    """
    rows = parse_quality_report(ALLMODELS_TABLE)
    assert len(rows) == 1
    # The specific model wins over the general one when both are present.
    assert rows[0].completeness == pytest.approx(94.75)
    assert rows[0].contamination == pytest.approx(2.10)


def test_contamination_above_100_is_not_clamped():
    """R6: a bin holding several organisms scores over 100% contamination.

    Clamping to 100 would hide the worst bins by making them look merely
    mediocre, which is the opposite of what the number is for.
    """
    table = (
        "Name\tCompleteness\tContamination\n"
        "bin.7\t88.00\t137.40\n"
    )
    rows = parse_quality_report(table)
    assert rows[0].contamination == pytest.approx(137.40)
    facts = bin_quality_facts(rows[0])
    assert facts["checkm2_contamination"] == pytest.approx(137.40)


def test_quality_score_can_go_negative():
    """completeness - 5x contamination, not floored.

    A heavily contaminated bin scoring below zero is meaningful; flooring it
    at zero would make it indistinguishable from a merely empty bin.
    """
    row = BinQuality(name="bin.7", completeness=88.0, contamination=137.4)
    assert row.quality_score == pytest.approx(-599.0)


def test_unscored_rows_are_dropped_not_fatal():
    """One unscored bin must not cost the others their scores."""
    table = (
        "Name\tCompleteness\tContamination\n"
        "bin.1\t95.0\t1.0\n"
        "bin.2\tNA\tNA\n"
        "bin.3\t70.0\t3.0\n"
    )
    rows = parse_quality_report(table)
    assert [r.name for r in rows] == ["bin.1", "bin.3"]


def test_parse_empty_report():
    assert parse_quality_report("") == []
    assert parse_quality_report("Name\tCompleteness\tContamination\n") == []


@pytest.mark.parametrize(
    "completeness,contamination,expected",
    [
        (95.0, 2.0, "high"),
        (90.0, 5.0, "high"),  # inclusive at the boundary
        (89.9, 2.0, "medium"),
        (90.0, 5.1, "medium"),
        (50.0, 10.0, "medium"),
        (49.9, 1.0, "low"),
        (95.0, 10.1, "low"),
    ],
)
def test_quality_tier_thresholds(completeness, contamination, expected):
    """The MIMAG conventions, as a label -- never used to filter (Q4/R5)."""
    assert quality_tier(completeness, contamination) == expected


def test_bin_quality_facts_are_flat_scalars():
    """Per-key facts, merged by `facts.<key>` path rather than a dict (#606)."""
    row = parse_quality_report(DEFAULT_TABLE)[0]
    facts = bin_quality_facts(row)
    assert facts["checkm2_completeness"] == pytest.approx(98.55)
    assert facts["checkm2_contamination"] == pytest.approx(1.20)
    assert facts["checkm2_quality_tier"] == "high"
    assert all(not isinstance(v, dict) for v in facts.values())
    assert all(k.startswith("checkm2_") for k in facts)


def test_quality_report_filename_is_pinned():
    assert checkm2_runner.QUALITY_REPORT == "quality_report.tsv"
