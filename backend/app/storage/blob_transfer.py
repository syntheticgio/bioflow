"""Blob transfer between compute nodes and the primary.

A compute node has its own empty ``/data`` (``BIOINFO_HOME``).  Pipeline jobs
need input files — references, reads, assemblies — that live on the primary.
Since every file is a content-addressed blob (SHA-256), the node fetches
individual blobs on demand from the primary's HTTP API.

Output blobs written by the node are pushed back to the primary so the
results applier (which runs on the primary) can ingest them.

All HTTP calls are synchronous (``urllib``) because pipeline handlers run
in thread / subprocess mode, and the GIL is released during I/O.
"""

from __future__ import annotations

import hashlib
import urllib.error
import urllib.request
from pathlib import Path

from app.config import settings
from app.logging import get_logger
from app.storage.paths import blob_path, validate_sha256

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def ensure_blob(digest: str) -> Path:
    """Make sure a content-addressed blob is present *on this machine*.

    Checks the local object store first.  When running on a compute node
    and the blob is missing, fetches it from the primary via HTTP GET.

    Returns the local path for the caller to use directly (handler code
    keeps calling ``blob_path`` — this just guarantees the file is there).
    """
    validate_sha256(digest)
    path = blob_path(digest)

    if path.is_file():
        return path

    if not settings.is_compute_node:
        # We are the primary — the blob genuinely does not exist.
        raise _blob_missing(digest)

    if not settings.primary_api_url:
        raise _no_primary_url()

    return _fetch(digest, path)


def push_blob(digest: str) -> bool:
    """Upload a local blob to the primary.

    No-op when this machine is the primary.  Skips the upload when the
    primary already has the blob (idempotent).

    Returns ``True`` if the blob was uploaded, ``False`` if it was already
    present on the primary.
    """
    validate_sha256(digest)

    if not settings.is_compute_node:
        return False

    if not settings.primary_api_url:
        raise _no_primary_url()

    path = blob_path(digest)
    if not path.is_file():
        raise _blob_missing(digest)

    return _push(digest, path)


def resolve_payload_digests(payload: dict) -> list[str]:
    """Ensure every content-addressed blob referenced in *payload* is local.

    Scans the payload dict (and any nested dicts/lists) for keys ending in
    ``_sha256``, validates each value as a SHA-256 digest, and calls
    ``ensure_blob`` for each one.

    Returns the list of digests that were fetched (empty when all were
    already local, or when this is the primary).
    """
    if not settings.is_compute_node:
        return []

    digests = _collect_digests(payload)
    fetched: list[str] = []
    for digest in digests:
        path = blob_path(digest)
        if path.is_file():
            continue
        if not settings.primary_api_url:
            raise _no_primary_url()
        _fetch(digest, path)
        fetched.append(digest)
    return fetched


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_BLOB_CHUNK = 256 * 1024  # 256 KiB read/write chunks


def _collect_digests(obj, seen=None):
    """Recursively collect SHA-256 digest values from a JSON-like structure."""
    if seen is None:
        seen = set()
    if isinstance(obj, dict):
        results = []
        for key, value in obj.items():
            if key.endswith("_sha256") and isinstance(value, str) and len(value) == 64:
                try:
                    validate_sha256(value)
                    if value not in seen:
                        seen.add(value)
                        results.append(value)
                except Exception:
                    pass
            else:
                results.extend(_collect_digests(value, seen))
        return results
    if isinstance(obj, list):
        results = []
        for item in obj:
            results.extend(_collect_digests(item, seen))
        return results
    return []


def _auth_header() -> dict[str, str]:
    """Return the ``X-Node-Secret`` header dict, or empty if no secret."""
    s = settings.node_shared_secret
    if s:
        return {"X-Node-Secret": s}
    return {}


def _fetch(digest: str, dest: Path) -> Path:
    """Download one blob from the primary and write it to *dest* atomically."""
    url = f"{settings.primary_api_url}/api/v1/objects/blob/{digest}"
    tmp = dest.with_suffix(dest.suffix + ".tmp")

    try:
        req = urllib.request.Request(url, headers=_auth_header())
        with urllib.request.urlopen(req, timeout=30) as resp:
            _check_status(resp, digest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            actual = hashlib.sha256()
            with open(tmp, "wb") as f:
                while chunk := resp.read(_BLOB_CHUNK):
                    f.write(chunk)
                    actual.update(chunk)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise _blob_not_on_primary(digest)
        raise _fetch_failed(digest, url, str(e))
    except OSError as e:
        # Network error, DNS failure, timeout — retryable.
        raise _fetch_failed(digest, url, str(e))

    if actual.hexdigest() != digest:
        tmp.unlink(missing_ok=True)
        raise _fetch_failed(digest, url, "SHA-256 mismatch after download")

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp.rename(dest)
    log.info("blob_fetched", digest=digest, size=dest.stat().st_size)
    return dest


def _push(digest: str, path: Path) -> bool:
    """Upload *path* to the primary via HTTP PUT.  Returns True if uploaded."""
    url = f"{settings.primary_api_url}/api/v1/objects/blob/{digest}"
    data = path.read_bytes()

    # Quick check: ask the primary if it already has this blob.
    # We use a HEAD request so we don't upload gigabytes unnecessarily.
    try:
        head_req = urllib.request.Request(url, headers=_auth_header(), method="HEAD")
        urllib.request.urlopen(head_req, timeout=10)
        # 200 = already exists → skip.
        return False
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise _push_failed(digest, url, str(e))
    except OSError:
        pass  # HEAD failed, try PUT anyway.

    # Validate the local file before uploading.
    actual = hashlib.sha256(data).hexdigest()
    if actual != digest:
        raise _push_failed(digest, url, f"local SHA-256 mismatch: expected {digest}, got {actual}")

    try:
        put_req = urllib.request.Request(
            url,
            data=data,
            headers={**_auth_header(), "Content-Type": "application/octet-stream"},
            method="PUT",
        )
        with urllib.request.urlopen(put_req, timeout=60) as resp:
            _check_status(resp, digest)
    except urllib.error.HTTPError as e:
        raise _push_failed(digest, url, str(e))
    except OSError as e:
        raise _push_failed(digest, url, str(e))

    log.info("blob_pushed", digest=digest, size=len(data))
    return True


def _check_status(resp, digest: str) -> None:
    """Raise if the HTTP response is not 2xx/3xx."""
    # urllib raises HTTPError for 4xx/5xx; this is a belt-and-suspenders
    # check for unexpected status codes that don't raise.
    if resp.status >= 400:
        raise _fetch_failed(digest, resp.url or "", f"HTTP {resp.status}")


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------


def _blob_missing(digest: str) -> FileNotFoundError:
    return FileNotFoundError(f"Blob not found locally: {digest}")


def _blob_not_on_primary(digest: str) -> FileNotFoundError:
    return FileNotFoundError(
        f"Blob {digest} not found on the primary — the data may have been deleted"
    )


def _no_primary_url() -> RuntimeError:
    return RuntimeError(
        "PRIMARY_API_URL is not set — a compute node needs the primary's API URL "
        "to fetch and push blobs"
    )


def _fetch_failed(digest: str, url: str, detail: str) -> OSError:
    return OSError(f"Failed to fetch blob {digest} from {url}: {detail}")


def _push_failed(digest: str, url: str, detail: str) -> OSError:
    return OSError(f"Failed to push blob {digest} to {url}: {detail}")
