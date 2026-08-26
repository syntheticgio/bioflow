"""Tests for blob_transfer — content-addressed blob fetch/push between nodes."""

import hashlib
import io
import urllib.error
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest

from app.config import Settings
from app.storage.blob_transfer import (
    _collect_digests,
    ensure_blob,
    push_blob,
    resolve_payload_digests,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _blob_path(dir: Path, digest: str) -> Path:
    """Mirror of blob_path but scoped to a test directory."""
    return dir / digest[:2] / digest


def _write_blob(dir: Path, digest: str, content: bytes) -> Path:
    """Write a blob to a test objects directory and return its path."""
    path = _blob_path(dir, digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _fake_200_response(content: bytes):
    """Return a mock HTTP response that reads *content*."""
    resp = BytesIO(content)
    resp.status = 200
    resp.url = "http://primary:8000/api/v1/objects/blob/abc"
    return resp


class _FakeResponse:
    """Minimal urllib response context manager (status + url only)."""

    def __init__(self, status: int, url: str = "http://primary:8000/api/v1/objects/blob/x"):
        self.status = status
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# _collect_digests
# ---------------------------------------------------------------------------


class TestCollectDigests:
    def test_empty_payload(self):
        assert _collect_digests({}) == []

    def test_single_digest_field(self):
        payload = {"reads_1_sha256": "a" * 64}
        assert _collect_digests(payload) == [payload["reads_1_sha256"]]

    def test_multiple_digest_fields(self):
        payload = {
            "reads_1_sha256": "a" * 64,
            "reads_2_sha256": "b" * 64,
            "reference_sha256": "c" * 64,
        }
        result = _collect_digests(payload)
        assert len(result) == 3

    def test_nested_dict(self):
        payload = {"params": {"reference_sha256": "a" * 64}}
        assert _collect_digests(payload) == ["a" * 64]

    def test_list_of_dicts(self):
        payload = {"outputs": [{"digest_sha256": "a" * 64}, {"digest_sha256": "b" * 64}]}
        result = _collect_digests(payload)
        assert len(result) == 2

    def test_deduplicates(self):
        payload = {"ref_sha256": "a" * 64, "other_ref_sha256": "a" * 64}
        assert _collect_digests(payload) == ["a" * 64]

    def test_skips_non_sha256_values(self):
        payload = {"reads_1_sha256": "a" * 64, "name": "sample.fastq", "count": 42}
        assert _collect_digests(payload) == ["a" * 64]

    def test_skips_invalid_hex(self):
        payload = {"reads_1_sha256": "z" * 64}
        assert _collect_digests(payload) == []

    def test_skips_wrong_length(self):
        payload = {"reads_1_sha256": "a" * 32}
        assert _collect_digests(payload) == []

    def test_key_without_sha256_suffix(self):
        payload = {"digest": "a" * 64}
        assert _collect_digests(payload) == []


# ---------------------------------------------------------------------------
# ensure_blob
# ---------------------------------------------------------------------------


class TestEnsureBlob:
    def test_returns_local_path_when_present(self, tmp_path):
        content = b"hello"
        digest = hashlib.sha256(content).hexdigest()
        path = _write_blob(tmp_path, digest, content)

        with patch("app.storage.blob_transfer.settings") as s:
            s.is_compute_node = False
            s.primary_api_url = ""
            with patch("app.storage.blob_transfer.blob_path", return_value=path):
                result = ensure_blob(digest)
                assert result == path

    def test_raises_when_missing_on_primary(self, tmp_path):
        digest = "a" * 64
        path = _blob_path(tmp_path, digest)

        with patch("app.storage.blob_transfer.settings") as s:
            s.is_compute_node = False
            s.primary_api_url = ""
            with patch("app.storage.blob_transfer.blob_path", return_value=path):
                with pytest.raises(FileNotFoundError):
                    ensure_blob(digest)

    def test_fetches_from_primary_when_missing(self, tmp_path):
        content = b"hello world"
        digest = hashlib.sha256(content).hexdigest()
        path = _blob_path(tmp_path, digest)
        path.parent.mkdir(parents=True, exist_ok=True)  # ensure parent dir exists

        with patch("app.storage.blob_transfer.settings") as s:
            s.is_compute_node = True
            s.primary_api_url = "http://primary:8000"
            s.node_shared_secret = ""

            with patch("app.storage.blob_transfer.blob_path", return_value=path):
                with patch("app.storage.blob_transfer.urllib.request.urlopen") as urlopen:
                    urlopen.return_value.__enter__.return_value = _fake_200_response(content)

                    result = ensure_blob(digest)
                    assert result == path
                    assert path.read_bytes() == content

    def test_validates_sha256_after_fetch(self, tmp_path):
        content = b"correct content"
        digest = hashlib.sha256(content).hexdigest()
        path = _blob_path(tmp_path, digest)
        path.parent.mkdir(parents=True, exist_ok=True)

        with patch("app.storage.blob_transfer.settings") as s:
            s.is_compute_node = True
            s.primary_api_url = "http://primary:8000"
            s.node_shared_secret = ""

            with patch("app.storage.blob_transfer.blob_path", return_value=path):
                with patch("app.storage.blob_transfer.urllib.request.urlopen") as urlopen:
                    urlopen.return_value.__enter__.return_value = _fake_200_response(
                        b"wrong content"
                    )

                    with pytest.raises(OSError, match="SHA-256 mismatch"):
                        ensure_blob(digest)

    def test_raises_when_no_primary_url(self, tmp_path):
        digest = "a" * 64
        path = _blob_path(tmp_path, digest)

        with patch("app.storage.blob_transfer.settings") as s:
            s.is_compute_node = True
            s.primary_api_url = ""
            with patch("app.storage.blob_transfer.blob_path", return_value=path):
                with pytest.raises(RuntimeError, match="PRIMARY_API_URL"):
                    ensure_blob(digest)


# ---------------------------------------------------------------------------
# resolve_payload_digests
# ---------------------------------------------------------------------------


class TestResolvePayloadDigests:
    def test_noop_when_not_compute_node(self):
        payload = {"reads_1_sha256": "a" * 64}
        with patch("app.storage.blob_transfer.settings") as s:
            s.is_compute_node = False
            assert resolve_payload_digests(payload) == []

    def test_skips_already_local_blobs(self, tmp_path):
        content = b"test"
        digest = hashlib.sha256(content).hexdigest()
        _write_blob(tmp_path, digest, content)
        payload = {"reads_1_sha256": digest}

        with patch("app.storage.blob_transfer.settings") as s:
            s.is_compute_node = True
            s.primary_api_url = "http://primary:8000"
            s.node_shared_secret = ""
            with patch("app.storage.blob_transfer.blob_path") as mock_bp:
                mock_bp.return_value = _blob_path(tmp_path, digest)
                assert resolve_payload_digests(payload) == []

    def test_fetches_missing_blobs(self, tmp_path):
        content = b"remote content"
        digest = hashlib.sha256(content).hexdigest()
        path = _blob_path(tmp_path, digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"reads_1_sha256": digest}

        with patch("app.storage.blob_transfer.settings") as s:
            s.is_compute_node = True
            s.primary_api_url = "http://primary:8000"
            s.node_shared_secret = ""

            with patch("app.storage.blob_transfer.blob_path", return_value=path):
                with patch("app.storage.blob_transfer.urllib.request.urlopen") as urlopen:
                    urlopen.return_value.__enter__.return_value = _fake_200_response(content)

                    fetched = resolve_payload_digests(payload)
                    assert fetched == [digest]
                    assert path.read_bytes() == content

    def test_handles_nested_params(self, tmp_path):
        content = b"nested"
        digest = hashlib.sha256(content).hexdigest()
        _write_blob(tmp_path, digest, content)
        payload = {"params": {"reference_sha256": digest}}

        with patch("app.storage.blob_transfer.settings") as s:
            s.is_compute_node = True
            s.primary_api_url = "http://primary:8000"
            s.node_shared_secret = ""
            with patch("app.storage.blob_transfer.blob_path") as mock_bp:
                mock_bp.return_value = _blob_path(tmp_path, digest)
                assert resolve_payload_digests(payload) == []


# ---------------------------------------------------------------------------
# push_blob
# ---------------------------------------------------------------------------


class TestPushBlob:
    def test_noop_when_primary(self):
        digest = "a" * 64
        with patch("app.storage.blob_transfer.settings") as s:
            s.is_compute_node = False
            assert push_blob(digest) is False

    def test_raises_when_blob_missing_locally(self, tmp_path):
        digest = "a" * 64
        path = _blob_path(tmp_path, digest)

        with patch("app.storage.blob_transfer.settings") as s:
            s.is_compute_node = True
            s.primary_api_url = "http://primary:8000"
            s.node_shared_secret = ""
            with patch("app.storage.blob_transfer.blob_path", return_value=path):
                with pytest.raises(FileNotFoundError):
                    push_blob(digest)

    def test_validates_local_sha256_before_push(self, tmp_path):
        content = b"actual"
        wrong_digest = hashlib.sha256(b"different").hexdigest()
        # Write content with the WRONG digest as its path.
        path = _write_blob(tmp_path, wrong_digest, content)

        with patch("app.storage.blob_transfer.settings") as s:
            s.is_compute_node = True
            s.primary_api_url = "http://primary:8000"
            s.node_shared_secret = ""
            with patch("app.storage.blob_transfer.blob_path", return_value=path):
                with pytest.raises(OSError, match="local SHA-256 mismatch"):
                    push_blob(wrong_digest)

    def test_push_streams_the_file_instead_of_buffering_it(self, tmp_path):
        """The whole OOM: read_bytes() on a multi-GB BAM loads it all into
        RAM. The PUT body must be a stream opened on the file with an
        explicit Content-Length, not an in-memory bytes buffer. The stream is
        closed by the time urlopen returns, so the body is snapshotted inside
        the fake urlopen -- the moment the real one reads it."""
        content = b"x" * (300 * 1024)  # > _BLOB_CHUNK, exercises chunked reads
        digest = hashlib.sha256(content).hexdigest()
        path = _write_blob(tmp_path, digest, content)

        captured = {}

        def fake_urlopen(req, timeout=None):
            if req.get_method() == "HEAD":
                raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, None)
            # Snapshot the body while the stream is still open.
            captured["data_type"] = type(req.data).__name__
            captured["is_stream"] = isinstance(req.data, io.IOBase)
            captured["content"] = req.data.read()
            captured["content_length"] = req.headers.get("Content-length")
            return _FakeResponse(200)

        with patch("app.storage.blob_transfer.settings") as s:
            s.is_compute_node = True
            s.primary_api_url = "http://primary:8000"
            s.node_shared_secret = ""
            with patch("app.storage.blob_transfer.blob_path", return_value=path):
                with patch(
                    "app.storage.blob_transfer.urllib.request.urlopen",
                    side_effect=fake_urlopen,
                ):
                    assert push_blob(digest) is True

        # The body was a stream (not an in-memory bytes buffer)...
        assert captured["is_stream"], f"body was {captured['data_type']}, not a stream"
        # ...with an explicit Content-Length...
        assert captured["content_length"] == str(len(content))
        # ...carrying exactly the file content.
        assert captured["content"] == content

    def test_fetch_failure_removes_the_partial_tmp_file(self, tmp_path):
        """The ticket's cleanup bug: _fetch only unlinked its .tmp partial on
        the digest-mismatch path. The write path -- where a partial .tmp
        actually exists -- never cleaned up, so a failed write (disk full,
        etc.) left {digest}.tmp files beside the store that nothing ever
        swept. A disk-full during the write must leave no partial behind."""
        import builtins
        import contextlib

        digest = "b" * 64
        path = _blob_path(tmp_path, digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        real_open = builtins.open

        # Response streams one chunk; writing it to the .tmp fails with a
        # disk-full, leaving a partial download behind that must be swept.
        class _Resp(contextlib.AbstractContextManager):
            status = 200
            url = "http://primary:8000/api/v1/objects/blob/" + digest

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, n):
                return b"partial chunk"

        def fake_open(file, mode="r", *args, **kwargs):
            if str(file) == str(tmp) and "w" in mode:
                tmp.write_bytes(b"partial")  # the download that must be swept
                raise OSError("No space left on device")
            return real_open(file, mode, *args, **kwargs)

        with patch("app.storage.blob_transfer.settings") as s:
            s.is_compute_node = True
            s.primary_api_url = "http://primary:8000"
            s.node_shared_secret = ""
            with patch("app.storage.blob_transfer.blob_path", return_value=path):
                with patch(
                    "app.storage.blob_transfer.urllib.request.urlopen",
                    side_effect=lambda req, timeout=None: _Resp(),
                ):
                    with patch("app.storage.blob_transfer.open", side_effect=fake_open):
                        with pytest.raises(OSError, match="No space left on device"):
                            ensure_blob(digest)

        assert not tmp.exists(), "failed write left the .tmp partial behind"
        assert not path.exists()

    def test_digest_mismatch_removes_the_tmp_file(self, tmp_path):
        """The pre-existing cleanup path, kept: a corrupt download must not
        be promoted to a real blob and must not leave its .tmp behind."""
        digest = "c" * 64
        path = _blob_path(tmp_path, digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")

        resp = BytesIO(b"corrupt content that won't match")
        resp.status = 200
        resp.url = "http://primary:8000/api/v1/objects/blob/" + digest

        with patch("app.storage.blob_transfer.settings") as s:
            s.is_compute_node = True
            s.primary_api_url = "http://primary:8000"
            s.node_shared_secret = ""
            with patch("app.storage.blob_transfer.blob_path", return_value=path):
                with patch(
                    "app.storage.blob_transfer.urllib.request.urlopen",
                    side_effect=lambda req, timeout=None: resp,
                ):
                    with pytest.raises(OSError, match="SHA-256 mismatch"):
                        ensure_blob(digest)

        assert not tmp.exists()
        assert not path.exists()


# ---------------------------------------------------------------------------
# Settings integration
# ---------------------------------------------------------------------------


class TestSettingsIsComputeNode:
    def test_default_is_primary(self):
        s = Settings()
        assert s.node_type == "primary"
        assert s.is_compute_node is False

    def test_compute_node(self):
        s = Settings(node_type="compute")
        assert s.is_compute_node is True
