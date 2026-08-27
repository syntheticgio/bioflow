"""Path construction and validation for the content-addressed store."""

import re
from pathlib import Path, PurePosixPath

from app.config import settings
from app.errors import NotFoundError, ValidationError

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


def resolve_job_input_path(path_str: str) -> Path:
    """Resolve a `*_path` value from a job payload, or refuse it.

    Job payloads are not trusted input. Anything that can enqueue a job could
    otherwise name any file the worker can read -- `/data/.biopipe/secret.key`,
    `/etc/passwd` -- and have a pipeline consume it, with the content
    potentially surfacing in outputs. The reachable route is the MCP
    `run_pipeline` tool, whose `params` become the payload verbatim, so a
    prompt-injected agent (file content is untrusted) could do exactly this.
    The normal UI launch path only writes digests, which are already validated.

    Wider than `resolve_registerable` by exactly BioFlow's own managed
    directories: a handler that fans out sub-jobs writes intermediate inputs
    under `tmp_dir` and passes them by path -- `align_reads_chunked` does this
    with its per-bucket reference FASTAs. Those are files BioFlow itself just
    wrote, so they are legitimate, but they are not register roots and would
    otherwise be refused as soon as BIOINFO_REGISTER_ROOTS was set to anything
    but its default.
    """
    candidate = Path(path_str)
    if not candidate.is_absolute():
        raise ValidationError("Job input path must be absolute")

    # Resolve before comparing, so a symlink pointing outside an allowed root
    # is rejected rather than followed.
    real = candidate.resolve()
    roots = [*settings.register_roots, *_managed_roots()]
    for root in roots:
        try:
            real.relative_to(root)
        except ValueError:
            continue
        return real

    raise ValidationError(
        f"Job input path is outside the allowed roots: {real}",
        details={"allowed_roots": [str(r) for r in roots]},
    )


def _managed_roots() -> list[Path]:
    """BioFlow's own directories, which handlers legitimately pass by path."""
    return [
        settings.objects_dir.resolve(),
        settings.staging_dir.resolve(),
        settings.tmp_dir.resolve(),
    ]


def resolve_report_file(root: Path, report_path: str) -> Path:
    """Resolve a client-supplied report path inside `root`, or raise.

    Three steps, in this order. Segments are rejected textually first, so a
    crafted path never reaches the filesystem. The result is then resolved and
    re-checked against the root, which is what catches a symlink whose target
    escapes the tree -- the one case the textual pass cannot see. Finally the
    target must be a regular file: that is what rejects the root directory
    itself, and it is load-bearing rather than decorative.

    Raises NotFoundError rather than a permission error for every rejection:
    the caller has already proven it owns the object, so the only thing a
    distinct status code would reveal is whether a given path exists.
    """
    parts = PurePosixPath(report_path).parts
    if any(p in ("..", "") for p in parts) or PurePosixPath(report_path).is_absolute():
        raise NotFoundError(f"No such report: {report_path}")

    target = (root / report_path).resolve()
    if not target.is_relative_to(root.resolve()) or not target.is_file():
        raise NotFoundError(f"No such report: {report_path}")

    return target
