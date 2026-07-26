"""Content-addressed storage: hashing, placement, dedup, quarantine."""

import hashlib
import threading

import pytest

from app.errors import JobCancelled, PayloadTooLargeError
from app.storage import cas


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Point BIOINFO_HOME at a temp dir for every test in this module."""
    for name in ("objects", "staging", "tmp"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr("app.config.settings.bioinfo_home", tmp_path)
    return tmp_path


class TestHashFile:
    def test_matches_hashlib(self, tmp_path):
        data = b"ACGT" * 10000
        p = tmp_path / "f.bin"
        p.write_bytes(data)
        digest, size = cas.hash_file(p)
        assert digest == hashlib.sha256(data).hexdigest()
        assert size == len(data)

    def test_empty_file(self, tmp_path):
        p = tmp_path / "empty"
        p.write_bytes(b"")
        digest, size = cas.hash_file(p)
        assert digest == hashlib.sha256(b"").hexdigest()
        assert size == 0

    def test_spans_multiple_read_buffers(self, tmp_path):
        data = b"x" * (cas.READ_BUFFER * 2 + 137)
        p = tmp_path / "big.bin"
        p.write_bytes(data)
        digest, size = cas.hash_file(p)
        assert digest == hashlib.sha256(data).hexdigest()
        assert size == len(data)

    def test_cancellation_is_honoured(self, tmp_path):
        """Cancellation is checked on an 8 MiB boundary, so the file must be
        large enough to cross one."""
        p = tmp_path / "big.bin"
        p.write_bytes(b"y" * (cas.CANCEL_CHECK_BYTES + cas.READ_BUFFER))
        event = threading.Event()
        event.set()
        with pytest.raises(JobCancelled):
            cas.hash_file(p, cancel_event=event)


class TestWriteStreamToTemp:
    def test_hashes_while_writing(self, home):
        chunks = [b"AAAA", b"BBBB", b"CCCC"]
        expected = hashlib.sha256(b"".join(chunks)).hexdigest()
        path, digest, size = cas.write_stream_to_temp(iter(chunks))
        assert digest == expected
        assert size == 12
        assert path.read_bytes() == b"AAAABBBBCCCC"

    def test_skips_empty_chunks(self, home):
        path, digest, size = cas.write_stream_to_temp(iter([b"AB", b"", b"CD"]))
        assert size == 4
        assert path.read_bytes() == b"ABCD"

    def test_temp_file_removed_when_producer_raises(self, home):
        """A failed upload must not leave a partial file behind."""

        def failing():
            yield b"partial"
            raise PayloadTooLargeError("too big")

        before = set(home.glob("tmp/*"))
        with pytest.raises(PayloadTooLargeError):
            cas.write_stream_to_temp(failing())
        assert set(home.glob("tmp/*")) == before


class TestPlaceBlob:
    def _staged(self, home, data: bytes):
        path, digest, size = cas.write_stream_to_temp(iter([data]))
        return path, digest, size

    def test_creates_sharded_path(self, home):
        path, digest, size = self._staged(home, b"hello world")
        result = cas.place_blob(path, digest, size)

        assert result.result is cas.PlacementResult.CREATED
        assert result.path.exists()
        assert result.path.parent.name == digest[:2]
        assert result.path.name == digest
        assert result.path.read_bytes() == b"hello world"
        assert not path.exists()  # source was moved, not copied

    def test_second_identical_write_deduplicates(self, home):
        p1, d1, s1 = self._staged(home, b"same content")
        cas.place_blob(p1, d1, s1)

        p2, d2, s2 = self._staged(home, b"same content")
        result = cas.place_blob(p2, d2, s2)

        assert result.result is cas.PlacementResult.DEDUP
        assert not p2.exists()  # the redundant copy was discarded
        assert len(list(home.glob("objects/*/*"))) == 1

    def test_placed_blob_is_read_only(self, home):
        path, digest, size = self._staged(home, b"immutable")
        result = cas.place_blob(path, digest, size)
        assert result.path.stat().st_mode & 0o222 == 0

    def test_size_mismatch_quarantines_rather_than_overwrites(self, home):
        """Same content-addressed name, different size, means the stored file is
        corrupt. It must be preserved as evidence, never silently replaced."""
        path, digest, size = self._staged(home, b"correct content")
        final = cas.place_blob(path, digest, size).path

        # Corrupt the stored blob out from under us.
        final.chmod(0o644)
        final.write_bytes(b"CORRUPTED - wrong length entirely")

        p2, d2, s2 = self._staged(home, b"correct content")
        result = cas.place_blob(p2, d2, s2)

        assert result.result is cas.PlacementResult.CREATED
        assert result.path.read_bytes() == b"correct content"
        quarantined = list(home.glob(".biopipe/quarantine/*"))
        assert len(quarantined) == 1
        assert b"CORRUPTED" in quarantined[0].read_bytes()

    def test_rejects_malformed_digest(self, home):
        path, _, size = self._staged(home, b"data")
        from app.errors import ValidationError

        with pytest.raises(ValidationError):
            cas.place_blob(path, "../escape", size)


class TestUnlinkBlob:
    def test_removes_read_only_file(self, home):
        path, digest, size = cas.write_stream_to_temp(iter([b"delete me"]))
        placed = cas.place_blob(path, digest, size)
        assert cas.unlink_blob(digest) is True
        assert not placed.path.exists()

    def test_missing_blob_is_not_an_error(self, home):
        assert cas.unlink_blob("b" * 64) is False
