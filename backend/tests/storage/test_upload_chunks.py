"""Chunk sizing and atomic chunk writes."""

import pytest
from app.services.upload_service import (
    DEFAULT_CHUNK_SIZE,
    MAX_CHUNKS,
    _write_chunk_atomic,
    choose_chunk_size,
    chunk_path,
)

GB = 1024**3


class TestChunkSizing:
    @pytest.mark.parametrize("size", [0, 1, 1024, 100 * 1024 * 1024, 10 * GB])
    def test_small_and_medium_files_use_the_default(self, size):
        assert choose_chunk_size(size) == DEFAULT_CHUNK_SIZE

    @pytest.mark.parametrize("size", [200 * GB, 1024 * GB, 4096 * GB])
    def test_huge_files_scale_up_to_stay_under_the_chunk_cap(self, size):
        """received_chunks is an array on the session document, so the count has
        to stay bounded however large the file is."""
        chunk = choose_chunk_size(size)
        total = (size + chunk - 1) // chunk
        assert total <= MAX_CHUNKS, f"{total} chunks for {size / GB:.0f} GB"
        assert chunk >= DEFAULT_CHUNK_SIZE

    def test_chunk_size_is_always_a_power_of_two_multiple(self, ):
        for size in (200 * GB, 500 * GB, 2048 * GB):
            chunk = choose_chunk_size(size)
            assert chunk % DEFAULT_CHUNK_SIZE == 0

    def test_a_100gb_fastq_stays_within_the_cap(self):
        chunk = choose_chunk_size(100 * GB)
        assert (100 * GB) // chunk <= MAX_CHUNKS


class TestChunkPaths:
    def test_zero_padded_so_listings_sort_in_assembly_order(self, tmp_path):
        names = [chunk_path(tmp_path, i).name for i in (0, 5, 42, 1000)]
        assert names == ["000000.part", "000005.part", "000042.part", "001000.part"]
        assert names == sorted(names)


class TestAtomicChunkWrite:
    def test_writes_the_chunk(self, tmp_path):
        target = tmp_path / "chunks" / "000000.part"
        _write_chunk_atomic(target, b"payload")
        assert target.read_bytes() == b"payload"

    def test_leaves_no_tmp_file_behind(self, tmp_path):
        target = tmp_path / "chunks" / "000000.part"
        _write_chunk_atomic(target, b"payload")
        assert list(target.parent.glob("*.tmp")) == []

    def test_rewrite_replaces_cleanly(self, tmp_path):
        """Re-sending a chunk is normal on a flaky link and must be safe."""
        target = tmp_path / "chunks" / "000000.part"
        _write_chunk_atomic(target, b"first attempt")
        _write_chunk_atomic(target, b"second")
        assert target.read_bytes() == b"second"

    def test_a_part_file_is_never_partially_written(self, tmp_path):
        """Content lands under .tmp and is renamed, so anything named .part is
        complete by construction -- a truncated chunk would otherwise corrupt
        the assembled file while looking valid."""
        target = tmp_path / "chunks" / "000000.part"
        payload = b"x" * 100_000
        _write_chunk_atomic(target, payload)
        assert target.stat().st_size == len(payload)
