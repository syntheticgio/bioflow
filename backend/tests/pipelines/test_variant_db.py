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
            rows=iter(["chr1\t100\tA\tG\t50.0\tPASS\t30\t0/1\t1/1\t0/0"]),
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
