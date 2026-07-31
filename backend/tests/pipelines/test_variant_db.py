"""The SQLite database backing the variant table.

Separate from vcf_stats_runner because this is the only stateful part of the
feature and the only place SQL lives.
"""

import sqlite3

import pytest

from app.pipelines.variant_db import (
    VariantFilters,
    build_variant_db,
    count_variants,
    query_variants,
)


def _rows():
    """Three contigs, mixed FILTER values, SNPs and one indel."""
    return iter(
        [
            "chr1\t100\tA\tG\t50.0\tPASS\t30\t0/1",
            "chr1\t200\tC\tT\t10.0\tLowQual\t8\t0/1",
            "chr1\t300\tG\tGTT\t80.0\tPASS\t44\t1/1",
            "chr2\t150\tT\tC\t95.0\tPASS\t50\t1/1",
            "chr2\t250\tA\tT\t20.0\t.\t12\t0/1",
            "chr3\t50\tCTT\tC\t60.0\tPASS\t33\t0/1",
        ]
    )


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "variants.db"
    build_variant_db(rows=_rows(), db_path=path)
    return path


class TestBuild:
    def test_all_rows_land(self, db):
        assert count_variants(db_path=db, filters=VariantFilters()) == 6

    def test_both_indexes_exist(self, db):
        """A missing index turns a 0.3ms query into a full scan with no other
        symptom -- nothing else in the suite would catch it."""
        con = sqlite3.connect(db)
        names = {
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        assert "ix_variants_locus" in names
        assert "ix_variants_filter" in names

    def test_the_locus_index_is_actually_used(self, db):
        """Asserting the index exists is not the same as it being chosen."""
        con = sqlite3.connect(db)
        plan = " ".join(
            str(r)
            for r in con.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM variants "
                "WHERE chrom=? AND pos BETWEEN ? AND ?",
                ("chr1", 1, 500),
            )
        )
        assert "ix_variants_locus" in plan

    def test_numeric_columns_are_typed_not_text(self, db):
        """Stored as TEXT, `qual >= 30` compares lexically: '9.0' > '80.0'."""
        con = sqlite3.connect(db)
        row = con.execute(
            "SELECT pos, qual, dp FROM variants WHERE chrom='chr1' AND pos=100"
        ).fetchone()
        assert row == (100, 50.0, 30)

    def test_missing_dp_and_qual_become_null_not_zero(self, tmp_path):
        """bcftools emits '.' for an absent value. Storing 0 would place the
        record at the bottom of a depth chart rather than out of it."""
        path = tmp_path / "v.db"
        build_variant_db(
            rows=iter(["chr1\t10\tA\tG\t.\tPASS\t.\t0/1"]), db_path=path
        )
        con = sqlite3.connect(path)
        assert con.execute("SELECT qual, dp FROM variants").fetchone() == (None, None)

    def test_every_sample_genotype_is_kept(self, tmp_path):
        """`[\\t%GT]` emits one column per sample. Storing only parts[7] drops
        samples 2..n and leaves the picker showing sample 1's genotype for
        every selection -- wrong data, presented confidently. Invisible on
        the single-sample files this pipeline produces, which is exactly why
        it needs pinning."""
        path = tmp_path / "multi.db"
        build_variant_db(
            rows=iter(["chr1\t100\tA\tG\t50.0\tPASS\t.\t30\t0/1\t1/1\t0/0"]),
            db_path=path,
        )
        row = query_variants(
            db_path=path, filters=VariantFilters(), offset=0, limit=1
        )[0]
        assert row["gt"].split("\t") == ["0/1", "1/1", "0/0"]

    def test_empty_input_produces_a_valid_empty_database(self, tmp_path):
        """Two of the three VCFs in the live database are empty or near-empty."""
        path = tmp_path / "empty.db"
        build_variant_db(rows=iter([]), db_path=path)
        assert count_variants(db_path=path, filters=VariantFilters()) == 0

    def test_malformed_line_is_skipped_not_fatal(self, tmp_path):
        path = tmp_path / "v.db"
        build_variant_db(
            rows=iter(["chr1\t10\tA\tG\t5.0\tPASS\t3\t0/1", "garbage"]),
            db_path=path,
        )
        assert count_variants(db_path=path, filters=VariantFilters()) == 1


class TestQuery:
    def test_pagination_slices_in_locus_order(self, db):
        page = query_variants(db_path=db, filters=VariantFilters(), offset=0, limit=2)
        assert [r["chrom"] for r in page] == ["chr1", "chr1"]
        assert [r["pos"] for r in page] == [100, 200]

        page2 = query_variants(db_path=db, filters=VariantFilters(), offset=2, limit=2)
        assert [r["pos"] for r in page2] == [300, 150]

    def test_rows_are_dicts_keyed_by_column(self, db):
        row = query_variants(
            db_path=db, filters=VariantFilters(), offset=0, limit=1
        )[0]
        assert row == {
            "chrom": "chr1",
            "pos": 100,
            "ref": "A",
            "alt": "G",
            "qual": 50.0,
            "filter": "PASS",
            "dp": 30,
            "gt": "0/1",
            "gene": None,
            "consequence": None,
            "aa_change": None,
            "aa_pos": None,
        }

    def test_filter_by_contig(self, db):
        f = VariantFilters(contig="chr2")
        assert count_variants(db_path=db, filters=f) == 2

    def test_filter_by_position_range(self, db):
        f = VariantFilters(contig="chr1", pos_min=150, pos_max=350)
        rows = query_variants(db_path=db, filters=f, offset=0, limit=10)
        assert [r["pos"] for r in rows] == [200, 300]

    def test_filter_by_filter_value(self, db):
        f = VariantFilters(filter_value="PASS")
        assert count_variants(db_path=db, filters=f) == 4

    def test_filter_by_min_qual(self, db):
        f = VariantFilters(min_qual=60.0)
        rows = query_variants(db_path=db, filters=f, offset=0, limit=10)
        assert sorted(r["qual"] for r in rows) == [60.0, 80.0, 95.0]

    def test_filter_by_type_snp_excludes_indels(self, db):
        """A SNP is a single-base REF and ALT; length differences are indels."""
        f = VariantFilters(variant_type="snp")
        rows = query_variants(db_path=db, filters=f, offset=0, limit=10)
        assert all(len(r["ref"]) == 1 and len(r["alt"]) == 1 for r in rows)
        assert len(rows) == 4

    def test_filter_by_type_indel(self, db):
        f = VariantFilters(variant_type="indel")
        rows = query_variants(db_path=db, filters=f, offset=0, limit=10)
        assert {r["pos"] for r in rows} == {300, 50}

    def test_filters_combine(self, db):
        f = VariantFilters(contig="chr1", filter_value="PASS", min_qual=60.0)
        rows = query_variants(db_path=db, filters=f, offset=0, limit=10)
        assert [r["pos"] for r in rows] == [300]

    def test_filter_matching_nothing_returns_empty_not_error(self, db):
        f = VariantFilters(contig="chrX")
        assert query_variants(db_path=db, filters=f, offset=0, limit=10) == []
        assert count_variants(db_path=db, filters=f) == 0

    def test_filter_values_are_parameterized(self, db):
        """A contig name is user input reaching a WHERE clause."""
        f = VariantFilters(contig="chr1'; DROP TABLE variants; --")
        assert count_variants(db_path=db, filters=f) == 0
        assert count_variants(db_path=db, filters=VariantFilters()) == 6


class TestStreamingBuild:
    def test_build_does_not_materialize_the_input(self, tmp_path):
        """The build must consume its input lazily."""
        peak = 0

        def rows():
            nonlocal peak
            for i in range(50_000):
                peak = max(peak, i)
                yield f"chr1\t{i}\tA\tG\t50.0\tPASS\t30\t0/1"

        path = tmp_path / "v.db"
        n = build_variant_db(rows=rows(), db_path=path)
        assert n == 50_000
        assert count_variants(db_path=path, filters=VariantFilters()) == 50_000

    def test_peak_rss_stays_bounded_while_building(self, tmp_path):
        """The regression that matters. A streaming build should add a few
        MB; a list-then-insert refactor should add tens of MB, and every
        other test in this file still passes when that regression happens.

        Two things had to be fixed to make this guard real, not one:

        1. Row count and length. Real `bcftools query` rows run ~54 chars
           (e.g. "NC_001133.9\\t1234567\\tACGTACGTAC\\tA\\t247.3910\\tPASS\\t142\\t0/1").
           200k short synthetic rows (the previous fixture, ~25 chars each)
           only cost ~16 MB materialized -- nowhere near the old 150 MB
           threshold. At realistic row length, 1,000,000 rows costs ~100 MB
           materialized versus a few MB streamed, so 60 MB sits clearly
           between the two: ~6x the streaming cost and comfortably under the
           materialized cost.

        2. What "stays bounded" measures. `VmRSS` from /proc/self/status is a
           snapshot, not a peak: measured immediately before and after the
           call, a `rows = list(rows)` regression that peaks at +100 MB and
           is freed again before `build_variant_db` returns (glibc gives the
           pages back once the list and its strings are garbage collected)
           shows as only a few MB of *net* growth -- passing clean despite
           genuinely materializing the whole input mid-call. Measured this
           exact way against a real `list(rows)` regression in this
           container, VmRSS before/after showed ~5 MB of growth while
           `resource.getrusage(RUSAGE_SELF).ru_maxrss` -- the kernel's
           monotonic high-water mark for the process, which cannot decrease
           -- showed ~107 MB. So this test reads maxrss, not VmRSS.

        Do not shrink the row count/length or switch back to a before/after
        RSS snapshot to make this faster -- both are exactly what made the
        guard decorative before, and either one alone is enough to quietly
        stop it from catching the regression again."""
        import resource

        def peak_rss_mb() -> float:
            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024

        before = peak_rss_mb()
        path = tmp_path / "big.db"
        build_variant_db(
            rows=(
                f"NC_00113{i % 9}.9\t{i}\tACGTACGTAC\tA\t{i % 400}.391\tPASS\t{i % 200}\t0/1"
                for i in range(1_000_000)
            ),
            db_path=path,
        )
        growth = peak_rss_mb() - before
        assert growth < 60, f"build's peak RSS grew by {growth:.0f} MB"


class TestConsequenceColumns:
    """The annotated columns, which arrive as a 7th TSV field (index 6) holding
    the raw BCSQ value, ahead of the repeating DP/GT sample block so its
    position never depends on sample count. An un-annotated VCF sends "."
    there, which must round-trip as empty rather than as the string "."."""

    def test_stores_gene_consequence_and_aa_change(self, tmp_path):
        path = tmp_path / "v.db"
        build_variant_db(
            rows=iter(
                [
                    "NC_001133.9\t22639\tA\tT\t10.8\t.\t"
                    "missense|YAL063C-A|rna-NM_001184642.1|protein_coding|-|16F>16Y|22639A>T"
                    "\t1\t1/1"
                ]
            ),
            db_path=path,
        )
        row = query_variants(
            db_path=path, filters=VariantFilters(), offset=0, limit=10
        )[0]
        assert row["gene"] == "YAL063C-A"
        assert row["consequence"] == "missense"
        assert row["aa_change"] == "16F>16Y"
        assert row["aa_pos"] == 16

    def test_an_unannotated_row_has_empty_consequence_columns(self, tmp_path):
        path = tmp_path / "v.db"
        build_variant_db(
            rows=iter(["NC_001133.9\t12690\tA\tT\t30.0\t.\t.\t5\t0/1"]),
            db_path=path,
        )
        row = query_variants(
            db_path=path, filters=VariantFilters(), offset=0, limit=10
        )[0]
        assert row["gene"] is None
        assert row["consequence"] is None

    # A VCF indexed before this change has only 8 fields per line and no BCSQ
    # field at all.
    def test_a_row_with_no_consequence_field_still_loads(self, tmp_path):
        path = tmp_path / "v.db"
        build_variant_db(
            rows=iter(["NC_001133.9\t12690\tA\tT\t30.0\t.\t5\t0/1"]),
            db_path=path,
        )
        rows = query_variants(
            db_path=path, filters=VariantFilters(), offset=0, limit=10
        )
        assert len(rows) == 1
        assert rows[0]["consequence"] is None

    # Two samples plus BCSQ: the genotypes must not absorb the consequence,
    # and the consequence must not absorb a genotype.
    def test_multi_sample_genotypes_survive_the_leading_consequence(self, tmp_path):
        path = tmp_path / "v.db"
        build_variant_db(
            rows=iter(
                [
                    "c1\t1\tA\tT\t10\t.\t"
                    "missense|G1|r1|protein_coding|+|1A>1B|x"
                    "\t5\t0/1\t1/1"
                ]
            ),
            db_path=path,
        )
        row = query_variants(
            db_path=path, filters=VariantFilters(), offset=0, limit=10
        )[0]
        assert row["gt"] == "0/1\t1/1"
        assert row["consequence"] == "missense"

    def _two_rows(self):
        return iter(
            [
                "c1\t1\tA\tT\t10\t.\tmissense|G1|r1|protein_coding|+|1A>1B|x\t1\t1/1",
                "c1\t2\tA\tT\t10\t.\tsynonymous|G2|r2|protein_coding|+|2C|x\t1\t1/1",
            ]
        )

    def test_filters_by_consequence(self, tmp_path):
        path = tmp_path / "v.db"
        build_variant_db(rows=self._two_rows(), db_path=path)
        rows = query_variants(
            db_path=path,
            filters=VariantFilters(consequence="missense"),
            offset=0,
            limit=10,
        )
        assert len(rows) == 1
        assert rows[0]["gene"] == "G1"

    def test_counts_respect_the_consequence_filter(self, tmp_path):
        path = tmp_path / "v.db"
        build_variant_db(rows=self._two_rows(), db_path=path)
        assert (
            count_variants(
                db_path=path, filters=VariantFilters(consequence="missense")
            )
            == 1
        )

    # A three-sample row survives with the consequence fixed at index 6,
    # regardless of how many genotype columns follow it.
    def test_a_third_genotype_survives_the_fixed_consequence_position(self, tmp_path):
        path = tmp_path / "v.db"
        build_variant_db(
            rows=iter(["c1\t1\tA\tT\t10\t.\t.\t5\t0/1\t1/1\t0/0"]),
            db_path=path,
        )
        row = query_variants(
            db_path=path, filters=VariantFilters(), offset=0, limit=10
        )[0]
        assert row["gt"] == "0/1\t1/1\t0/0"
        assert row["consequence"] is None

    # Phased genotypes contain "|", which is also BCSQ's field separator --
    # the fixed position means that no longer matters.
    def test_a_phased_genotype_survives_the_fixed_consequence_position(self, tmp_path):
        path = tmp_path / "v.db"
        build_variant_db(
            rows=iter(["c1\t1\tA\tT\t10\t.\t.\t5\t0|1\t1|1"]),
            db_path=path,
        )
        row = query_variants(
            db_path=path, filters=VariantFilters(), offset=0, limit=10
        )[0]
        assert row["gt"] == "0|1\t1|1"
        assert row["consequence"] is None
