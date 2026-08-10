import pytest
from app.pipelines.align_buckets import BucketSpec, pack_buckets, write_bucket_fastas


class TestPackBuckets:
    def test_single_sequence_returns_none(self):
        result = pack_buckets(
            sequences=[("chr1", 100_000_000)],
            memory_budget_mb=4096,
            per_base_index_mb=3.2 / (1024 * 1024),
        )
        assert result is None

    def test_empty_returns_none(self):
        result = pack_buckets(
            sequences=[],
            memory_budget_mb=4096,
            per_base_index_mb=0.000003,
        )
        assert result is None

    def test_two_large_sequences_make_two_buckets(self):
        result = pack_buckets(
            sequences=[("chr1", 1_500_000_000), ("chr2", 1_200_000_000)],
            memory_budget_mb=12288,  # 12 GB — each alone fits, together they don't
            per_base_index_mb=3.2 / (1024 * 1024),
            fixed_overhead_mb=512,
            bytes_per_thread_mb=256,
            threads=4,
            sort_memory_mb=1024,
        )
        assert result is not None
        assert len(result) == 2
        assert result[0].sequences == ["chr1"]
        assert result[1].sequences == ["chr2"]

    def test_small_sequences_pack_together(self):
        result = pack_buckets(
            sequences=[(f"ctg{i}", 10_000_000) for i in range(100)],
            memory_budget_mb=2048,
            per_base_index_mb=3.2 / (1024 * 1024),
        )
        assert result is not None
        assert len(result) < 100

    def test_each_bucket_stays_under_budget(self):
        result = pack_buckets(
            sequences=[(f"ctg{i}", 50_000_000) for i in range(200)],  # 200 × 50M bases
            memory_budget_mb=8192,  # 8 GB
            per_base_index_mb=3.2 / (1024 * 1024),
            fixed_overhead_mb=256,
            bytes_per_thread_mb=256,
            threads=2,
            sort_memory_mb=512,
        )
        assert result is not None
        for bucket in result:
            assert bucket.estimated_mb <= 8192

    def test_tiny_budget_still_produces_single_bucket(self):
        result = pack_buckets(
            sequences=[("a", 1_000), ("b", 2_000), ("c", 3_000)],
            memory_budget_mb=1,
            per_base_index_mb=0.0,
        )
        # Effective budget is negative → single bucket returned as list
        assert result is not None
        assert len(result) == 1
        assert len(result[0].sequences) == 3


class TestWriteBucketFastas:
    def test_creates_correct_files(self, tmp_path):
        fasta = tmp_path / "ref.fa"
        fasta.write_text(">chr1\nAAAA\n>chr2\nGGGG\n")

        buckets = [
            BucketSpec(index=0, sequences=["chr1"], total_bases=4, estimated_mb=100),
            BucketSpec(index=1, sequences=["chr2"], total_bases=4, estimated_mb=100),
        ]

        out_dir = tmp_path / "buckets"
        result = write_bucket_fastas(fasta, buckets, out_dir)

        assert len(result) == 2
        assert result[0].fasta_path == out_dir / "bucket_0.fa"
        assert result[1].fasta_path == out_dir / "bucket_1.fa"
        assert ">chr1" in result[0].fasta_path.read_text()
        assert "AAAA" in result[0].fasta_path.read_text()
        assert ">chr2" in result[1].fasta_path.read_text()
        assert "GGGG" in result[1].fasta_path.read_text()
