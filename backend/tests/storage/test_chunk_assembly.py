"""Chunk assembly: correctness, idempotence, cancellation, integrity."""

import hashlib
import threading

import pytest
from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from app.errors import JobCancelled, PermanentError
from app.storage.chunk_assembly import assemble_and_hash


def write_chunks(tmp_path, pieces: list[bytes]) -> list:
    d = tmp_path / "chunks"
    d.mkdir(exist_ok=True)
    paths = []
    for i, piece in enumerate(pieces):
        p = d / f"{i:06d}.part"
        p.write_bytes(piece)
        paths.append(p)
    return paths


class TestAssembly:
    def test_concatenates_in_order(self, tmp_path):
        pieces = [b"AAAA", b"BBBB", b"CCCC"]
        paths = write_chunks(tmp_path, pieces)
        target = tmp_path / "out.bin"

        digest, size = assemble_and_hash(paths, target)

        assert target.read_bytes() == b"AAAABBBBCCCC"
        assert size == 12
        assert digest == hashlib.sha256(b"AAAABBBBCCCC").hexdigest()

    def test_digest_matches_a_separate_hash_of_the_result(self, tmp_path):
        """The single-pass optimization must not change the answer."""
        pieces = [bytes([i % 256]) * 5000 for i in range(7)]
        paths = write_chunks(tmp_path, pieces)
        target = tmp_path / "out.bin"

        digest, _ = assemble_and_hash(paths, target)
        assert digest == hashlib.sha256(target.read_bytes()).hexdigest()

    def test_single_chunk(self, tmp_path):
        paths = write_chunks(tmp_path, [b"only"])
        digest, size = assemble_and_hash(paths, tmp_path / "out.bin")
        assert size == 4
        assert digest == hashlib.sha256(b"only").hexdigest()

    def test_empty_chunks_are_harmless(self, tmp_path):
        paths = write_chunks(tmp_path, [b"AB", b"", b"CD"])
        digest, size = assemble_and_hash(paths, tmp_path / "out.bin")
        assert size == 4
        assert digest == hashlib.sha256(b"ABCD").hexdigest()

    def test_spans_multiple_read_buffers(self, tmp_path):
        from app.storage.chunk_assembly import READ_BUFFER

        pieces = [b"x" * (READ_BUFFER + 1234), b"y" * 4096]
        paths = write_chunks(tmp_path, pieces)
        digest, size = assemble_and_hash(paths, tmp_path / "out.bin")
        assert size == sum(len(p) for p in pieces)
        assert digest == hashlib.sha256(b"".join(pieces)).hexdigest()


class TestIdempotence:
    def test_rerun_produces_identical_output(self, tmp_path):
        """At-least-once delivery means assembly can run twice for one job."""
        paths = write_chunks(tmp_path, [b"AAAA", b"BBBB"])
        target = tmp_path / "out.bin"

        d1, s1 = assemble_and_hash(paths, target)
        d2, s2 = assemble_and_hash(paths, target)

        assert (d1, s1) == (d2, s2)
        assert target.read_bytes() == b"AAAABBBB"

    def test_truncates_a_partial_previous_attempt(self, tmp_path):
        """The regression this guards: appending instead of truncating would
        silently double the file after a crash-and-retry."""
        paths = write_chunks(tmp_path, [b"AAAA", b"BBBB"])
        target = tmp_path / "out.bin"
        target.write_bytes(b"GARBAGE FROM A CRASHED ATTEMPT" * 10)

        digest, size = assemble_and_hash(paths, target)

        assert size == 8
        assert target.read_bytes() == b"AAAABBBB"
        assert digest == hashlib.sha256(b"AAAABBBB").hexdigest()


class TestFailureModes:
    def test_missing_chunk_is_a_permanent_error(self, tmp_path):
        """No number of retries makes an absent chunk appear."""
        paths = write_chunks(tmp_path, [b"AAAA", b"BBBB"])
        paths[1].unlink()

        with pytest.raises(PermanentError, match="Chunk missing"):
            assemble_and_hash(paths, tmp_path / "out.bin")

    def test_missing_chunk_detected_before_any_writing(self, tmp_path):
        paths = write_chunks(tmp_path, [b"AAAA", b"BBBB"])
        paths[1].unlink()
        target = tmp_path / "out.bin"

        with pytest.raises(PermanentError):
            assemble_and_hash(paths, target)
        assert not target.exists()

    def test_cancellation_is_honoured(self, tmp_path):
        from app.storage.chunk_assembly import CANCEL_CHECK_BYTES

        paths = write_chunks(tmp_path, [b"z" * (CANCEL_CHECK_BYTES + 8192)])
        event = threading.Event()
        event.set()

        with pytest.raises(JobCancelled):
            assemble_and_hash(paths, tmp_path / "out.bin", cancel_event=event)


class TestProgress:
    def test_reports_final_byte_count(self, tmp_path):
        paths = write_chunks(tmp_path, [b"A" * 1000, b"B" * 1000])
        seen: list[int] = []
        _, size = assemble_and_hash(
            paths, tmp_path / "out.bin", progress_cb=seen.append
        )
        assert seen and seen[-1] == size == 2000


class TestChunkPermutations:
    @hyp_settings(max_examples=40, deadline=None)
    @given(
        pieces=st.lists(
            st.binary(min_size=0, max_size=512), min_size=1, max_size=12
        )
    )
    def test_assembly_always_equals_plain_concatenation(self, tmp_path_factory, pieces):
        """Whatever the chunk sizes, the result is the concatenation and the
        digest matches -- the guarantee a resumed upload depends on."""
        tmp_path = tmp_path_factory.mktemp("perm")
        paths = write_chunks(tmp_path, pieces)
        target = tmp_path / "out.bin"

        digest, size = assemble_and_hash(paths, target)
        expected = b"".join(pieces)

        assert target.read_bytes() == expected
        assert size == len(expected)
        assert digest == hashlib.sha256(expected).hexdigest()

    @hyp_settings(max_examples=20, deadline=None)
    @given(
        data=st.binary(min_size=1, max_size=4096),
        n_chunks=st.integers(min_value=1, max_value=16),
    )
    def test_any_split_of_the_same_data_yields_the_same_digest(
        self, tmp_path_factory, data, n_chunks
    ):
        """Chunk size is a transport detail; it must not affect the content
        address, or a resumed upload could produce a different blob id."""
        tmp_path = tmp_path_factory.mktemp("split")
        step = max(1, len(data) // n_chunks)
        pieces = [data[i : i + step] for i in range(0, len(data), step)] or [b""]
        paths = write_chunks(tmp_path, pieces)

        digest, size = assemble_and_hash(paths, tmp_path / "out.bin")

        assert size == len(data)
        assert digest == hashlib.sha256(data).hexdigest()
