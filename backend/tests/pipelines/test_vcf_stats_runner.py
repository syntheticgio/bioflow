"""Command construction and output parsing for VCF results statistics.

Pure functions over strings and paths, mirroring bam_stats_runner.py.

The STATS fixture is real `bcftools stats` output captured from
DRR1066343.bcftools.vcf.gz (6,641 variants, 17 contigs, S. cerevisiae).
"""

from pathlib import Path

from app.pipelines.vcf_stats_runner import (
    build_query_command,
    build_stats_command,
    parse_stats,
    rebin_distribution,
)

STATS = """# This file was produced by bcftools stats
# SN\t[2]id\t[3]key\t[4]value
SN\t0\tnumber of samples:\t1
SN\t0\tnumber of records:\t6641
SN\t0\tnumber of no-ALTs:\t0
SN\t0\tnumber of SNPs:\t6157
SN\t0\tnumber of MNPs:\t0
SN\t0\tnumber of indels:\t484
SN\t0\tnumber of others:\t0
SN\t0\tnumber of multiallelic sites:\t22
SN\t0\tnumber of multiallelic SNP sites:\t0
# TSTV\t[2]id\t[3]ts\t[4]tv\t[5]ts/tv
TSTV\t0\t4358\t1799\t2.42\t4358\t1799\t2.42
# ST\t[2]id\t[3]type\t[4]count
ST\t0\tA>C\t221
ST\t0\tA>G\t1109
ST\t0\tC>T\t1097
# QUAL\t[2]id\t[3]Quality\t[4]number of SNPs
QUAL\t0\t3.0\t2\t1\t1\t2
QUAL\t0\t3.1\t2\t1\t1\t4
QUAL\t0\t50.5\t19\t11\t8\t1
# DP\t[2]id\t[3]bin\t[4]number of genotypes
DP\t0\t1\t0\t0.000000\t1099\t16.548713
DP\t0\t2\t0\t0.000000\t753\t11.338654
# IDD\t[2]id\t[3]length
IDD\t0\t-2\t14\t0\t0.00
IDD\t0\t1\t31\t0\t0.00
"""

EMPTY_STATS = """# This file was produced by bcftools stats
# SN\t[2]id\t[3]key\t[4]value
SN\t0\tnumber of samples:\t1
SN\t0\tnumber of records:\t0
SN\t0\tnumber of SNPs:\t0
SN\t0\tnumber of indels:\t0
# TSTV\t[2]id\t[3]ts\t[4]tv\t[5]ts/tv
TSTV\t0\t0\t0\t0.00\t0\t0\t0.00
"""


class TestCommandConstruction:
    def test_stats_command(self):
        cmd = build_stats_command(
            bcftools_path="/usr/bin/bcftools", vcf=Path("/work/a.vcf.gz")
        )
        assert cmd == ["/usr/bin/bcftools", "stats", "/work/a.vcf.gz"]

    def test_query_command_emits_the_table_columns(self):
        """The format string is the schema: its field order must match
        VARIANT_COLUMNS or the database is populated with shifted values."""
        cmd = build_query_command(
            bcftools_path="/usr/bin/bcftools", vcf=Path("/work/a.vcf.gz")
        )
        assert cmd[:3] == ["/usr/bin/bcftools", "query", "-f"]
        # Real tabs and a real newline, not the two-character sequences. Write
        # this assertion with actual escapes -- "\t" not "\\t" -- because a
        # literal backslash-t makes bcftools emit one unsplittable column and
        # every row lands in the database as a single field.
        assert cmd[3] == "%CHROM\t%POS\t%REF\t%ALT\t%QUAL\t%FILTER\t%INFO/DP[\t%GT]\n"
        assert cmd[4] == "/work/a.vcf.gz"

    def test_query_format_separator_is_a_real_tab(self):
        """Guards the escaping directly: verified against bcftools 1.21, this
        format yields exactly 8 tab-separated columns."""
        from app.pipelines.vcf_stats_runner import QUERY_FORMAT

        assert "\t" in QUERY_FORMAT
        assert "\\t" not in QUERY_FORMAT
        assert QUERY_FORMAT.endswith("\n")


class TestParseStats:
    def test_sn_section_becomes_typed_counts(self):
        out = parse_stats(STATS)
        assert out["sn"]["records"] == 6641
        assert out["sn"]["snps"] == 6157
        assert out["sn"]["indels"] == 484
        assert out["sn"]["samples"] == 1
        assert out["sn"]["multiallelic_sites"] == 22

    def test_tstv_section(self):
        out = parse_stats(STATS)
        assert out["tstv"] == {"ts": 4358, "tv": 1799, "ti_tv": 2.42}

    def test_substitutions_preserve_order_and_counts(self):
        out = parse_stats(STATS)
        assert out["st"] == [
            {"type": "A>C", "count": 221},
            {"type": "A>G", "count": 1109},
            {"type": "C>T", "count": 1097},
        ]

    def test_qual_rows_are_value_and_count(self):
        """Column 4 is the SNP count at that QUAL; the trailing columns are
        transitions/transversions/indels, which the histogram does not use."""
        out = parse_stats(STATS)
        assert out["qual"] == [
            {"qual": 3.0, "count": 2},
            {"qual": 3.1, "count": 2},
            {"qual": 50.5, "count": 19},
        ]

    def test_dp_rows_use_the_number_of_sites_column(self):
        """Column 6 is number of sites. Column 4 is number of genotypes, which
        is 0 for a file bcftools did not genotype -- using it would draw an
        empty depth chart for a file that plainly has depth."""
        out = parse_stats(STATS)
        assert out["dp"] == [
            {"depth": 1, "count": 1099},
            {"depth": 2, "count": 753},
        ]

    def test_idd_rows(self):
        out = parse_stats(STATS)
        assert out["idd"] == [
            {"length": -2, "count": 14},
            {"length": 1, "count": 31},
        ]

    def test_comment_lines_are_skipped(self):
        out = parse_stats(STATS)
        assert all(not k.startswith("#") for k in out)

    def test_empty_vcf_parses_to_zeroes_rather_than_raising(self):
        """Two of the three VCFs in the live database hold 0 and 1 records.
        bcftools exits 0 and emits headers with no rows; that is a normal
        outcome of a strict caller, not a failure."""
        out = parse_stats(EMPTY_STATS)
        assert out["sn"]["records"] == 0
        assert out["st"] == []
        assert out["qual"] == []
        assert out["dp"] == []

    def test_unknown_sections_are_ignored(self):
        """bcftools adds sections between releases; an unrecognised one must
        not break parsing."""
        out = parse_stats(STATS + "HWE\t0\t1\t2\t3\n")
        assert out["sn"]["records"] == 6641


class TestRebinDistribution:
    def test_collapses_to_at_most_bucket_count(self):
        """805 QUAL rows on the real test file; facts stores a histogram."""
        rows = [{"qual": float(i), "count": 1} for i in range(805)]
        out = rebin_distribution(rows, value_key="qual", bucket_count=20)
        assert len(out) <= 20

    def test_preserves_the_total_count(self):
        """Re-binning redistributes; it must not lose or invent observations."""
        rows = [{"qual": float(i), "count": i} for i in range(100)]
        out = rebin_distribution(rows, value_key="qual", bucket_count=10)
        assert sum(b["count"] for b in out) == sum(r["count"] for r in rows)

    def test_buckets_span_the_observed_range(self):
        rows = [{"qual": 10.0, "count": 1}, {"qual": 90.0, "count": 1}]
        out = rebin_distribution(rows, value_key="qual", bucket_count=8)
        assert out[0]["value"] == 10.0
        assert out[-1]["value"] <= 90.0
        assert sum(b["count"] for b in out) == 2

    def test_single_distinct_value_yields_one_bucket(self):
        """A file where every record has the same QUAL must not divide by a
        zero-width range."""
        rows = [{"qual": 50.0, "count": 7}]
        out = rebin_distribution(rows, value_key="qual", bucket_count=20)
        assert out == [{"value": 50.0, "count": 7}]

    def test_empty_input_returns_empty(self):
        assert rebin_distribution([], value_key="qual", bucket_count=20) == []

    def test_fewer_rows_than_buckets_passes_through(self):
        rows = [{"depth": 1, "count": 5}, {"depth": 2, "count": 3}]
        out = rebin_distribution(rows, value_key="depth", bucket_count=20)
        assert sum(b["count"] for b in out) == 8
        assert len(out) == 2
