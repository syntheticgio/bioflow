"""Path construction and validation for the content-addressed store."""

import re
from pathlib import Path

from app.config import settings
from app.errors import ValidationError

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def validate_sha256(digest: str) -> str:
    """Normalize and validate a hex digest.

    Digests become filesystem paths, so this is also the guard that stops a
    crafted digest from escaping the object store.
    """
    normalized = digest.strip().lower()
    if not SHA256_RE.match(normalized):
        raise ValidationError(f"Not a valid SHA-256 hex digest: {digest!r}")
    return normalized


def blob_rel_path(digest: str) -> str:
    """Two-level sharding: 'abcdef...' -> 'ab/abcdef...'.

    A flat directory of hundreds of thousands of entries is slow to stat on any
    filesystem and markedly worse across the VirtioFS boundary.
    """
    d = validate_sha256(digest)
    return f"{d[:2]}/{d}"


def blob_path(digest: str) -> Path:
    return settings.objects_dir / blob_rel_path(digest)


def staging_dir_for(session_id: str) -> Path:
    # session_id comes from an ObjectId, but validate anyway: it reaches mkdir.
    if not re.match(r"^[0-9a-zA-Z_-]{1,64}$", session_id):
        raise ValidationError(f"Unsafe session id: {session_id!r}")
    return settings.staging_dir / session_id


def resolve_registerable(path_str: str) -> Path:
    """Resolve a register-in-place path, refusing anything outside the allowlist.

    Resolution happens *before* the containment check so that symlinks pointing
    outside an allowed root are rejected rather than followed.
    """
    candidate = Path(path_str)
    if not candidate.is_absolute():
        raise ValidationError("Register path must be absolute")

    real = candidate.resolve()
    roots = settings.register_roots
    for root in roots:
        try:
            real.relative_to(root)
        except ValueError:
            continue
        return real

    raise ValidationError(
        f"Path is outside the allowed roots: {real}",
        details={"allowed_roots": [str(r) for r in roots]},
    )
