"""Compress a plaintext file at ingest, hashing both streams in one pass.

Compression happens at the ingest policy point rather than per-producer -- see
docs/superpowers/specs/2026-08-05-object-compression-design.md. This module is
the seam: given a plaintext file already on disk, it decides whether the
format is worth compressing and, if so, produces a bgzip'd copy alongside two
hashes -- one of the plaintext (for dedup) and one of the compressed bytes
(the CAS key the compressed copy will be placed under).

Synchronous by design, like the rest of `storage/`; callers run it off the
event loop.
"""

import gzip
import hashlib
import os
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from app.config import settings
from app.errors import JobCancelled
from app.logging import get_logger
from app.models import Compression, FormatKind
from app.pipelines import tools

log = get_logger(__name__)

# Formats worth bgzip'ing at ingest. An allowlist rather than a denylist: a
# kind not in this set is left alone by construction, which is what makes an
# aligner index (detected as UNKNOWN -- see storage/detect.py) safe without
# enumerating every index extension here. BAM/CRAM/BCF are already
# block-compressed; FAI is read as a plain-text sidecar by samtools and
# indexes nothing itself; aligner indexes are mmap'd or randomly read.
COMPRESSIBLE_KINDS = frozenset(
    {
        FormatKind.FASTQ,
        FormatKind.FASTA,
        FormatKind.VCF,
        FormatKind.SAM,
        FormatKind.GFF,
        FormatKind.GTF,
        FormatKind.GENBANK,
        FormatKind.BED,
        FormatKind.GFA,
    }
)

# Matches cas.py: large enough to amortize syscall overhead, small enough not
# to pressure the page cache across a FUSE/VirtioFS mount.
READ_BUFFER = 4 * 1024 * 1024
CANCEL_CHECK_BYTES = 8 * 1024 * 1024

# bgzip's own default. Measured on a real 437 MB FASTQ from the store: 18%
# smaller than -1 for 0.3s more wall clock at 20 threads, and -9 buys a
# further 5.7% for roughly 4x the time. See the design doc for the full table.
COMPRESS_LEVEL = 6


def should_compress(kind: FormatKind, compression: Compression) -> bool:
    """Whether ingest should bgzip this file, per the design's allowlist.

    `compression` guards against double-compressing something that already
    arrived compressed (a user-supplied `.fastq.gz`, or output a runner like
    fastp already gzipped) -- `kind != NONE` is left alone unconditionally,
    never re-wrapped.
    """
    return kind in COMPRESSIBLE_KINDS and compression is Compression.NONE


@dataclass
class CompressionResult:
    path: Path  # the bgzip'd file, still under its caller-chosen temp name
    content_sha256: str  # hash of the plaintext that was read
    compressed_sha256: str  # hash of the bytes written -- becomes the CAS key
    compressed_size: int
    seekable: bool  # False when the stdlib fallback wrote plain gzip, not BGZF


def compress_and_hash(
    source: Path,
    dest_dir: Path | None = None,
    *,
    cancel_event: threading.Event | None = None,
) -> CompressionResult:
    """Bgzip `source` into a new temp file, hashing plaintext and output in one pass.

    `source` is read, never modified or removed -- placement decisions (dedup
    vs. store) are the caller's, made after seeing both hashes. Prefers the
    `bgzip` binary (multi-threaded, block-seekable output); falls back to
    Python's stdlib `gzip` when the binary is unavailable, which is correct
    but slower and produces plain gzip rather than BGZF -- `seekable=False`
    tells the caller so, since a caller compressing FASTA or VCF needs to know
    the `.fai`/tabix seek requirement was not met.
    """
    dest_dir = dest_dir or settings.tmp_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    # mkstemp only claims a unique name atomically; the actual writing happens
    # through a fresh `open(dest, ...)` in each compress helper below, so the
    # fd it hands back is closed immediately rather than carried along unused.
    fd, tmp_name = tempfile.mkstemp(dir=dest_dir, prefix="bgzip-", suffix=".part")
    os.close(fd)
    dest = Path(tmp_name)

    bgzip_tool = tools.bgzip()
    try:
        if bgzip_tool.available:
            content_sha256, compressed_sha256, compressed_size = _compress_with_bgzip(
                source, dest, bgzip_tool.path, cancel_event=cancel_event
            )
            seekable = True
        else:
            log.warning("bgzip_unavailable_falling_back_to_stdlib_gzip", source=str(source))
            content_sha256, compressed_sha256, compressed_size = _compress_with_stdlib_gzip(
                source, dest, cancel_event=cancel_event
            )
            seekable = False
    except BaseException:
        dest.unlink(missing_ok=True)
        raise

    return CompressionResult(
        path=dest,
        content_sha256=content_sha256,
        compressed_sha256=compressed_sha256,
        compressed_size=compressed_size,
        seekable=seekable,
    )


def _compress_with_bgzip(
    source: Path,
    dest: Path,
    bgzip_path: str,
    *,
    cancel_event: threading.Event | None,
) -> tuple[str, str, int]:
    """Pipe `source` through `bgzip -c`, hashing plaintext in and compressed out.

    `-k` (keep) is not needed since bgzip reads from stdin and never touches a
    named input file; passed no filename argument at all, only `-c` for
    stdout. Threaded with the process's CPU count -- ingest is CPU-bound once
    the mount has delivered the bytes, so more threads shortens the one thing
    the user is watching a progress bar for.
    """
    proc = subprocess.Popen(  # noqa: S603
        [bgzip_path, "-c", "-l", str(COMPRESS_LEVEL), "-@", str(os.cpu_count() or 1)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None

    plain_hash = hashlib.sha256()
    compressed_hash = hashlib.sha256()
    compressed_size = 0
    since_check = 0

    def _drain_stdout():
        nonlocal compressed_size
        with open(dest, "wb") as out:
            while chunk := proc.stdout.read(READ_BUFFER):
                out.write(chunk)
                compressed_hash.update(chunk)
                compressed_size += len(chunk)

    reader = threading.Thread(target=_drain_stdout, daemon=True)
    reader.start()

    try:
        with open(source, "rb") as f:
            while chunk := f.read(READ_BUFFER):
                plain_hash.update(chunk)
                since_check += len(chunk)
                if since_check >= CANCEL_CHECK_BYTES:
                    since_check = 0
                    if cancel_event is not None and cancel_event.is_set():
                        raise JobCancelled("Cancelled during compression")
                proc.stdin.write(chunk)
    finally:
        proc.stdin.close()
        reader.join()
        returncode = proc.wait()

    if returncode != 0:
        stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
        raise RuntimeError(f"bgzip exited {returncode}: {stderr.strip()}")

    return plain_hash.hexdigest(), compressed_hash.hexdigest(), compressed_size


def _compress_with_stdlib_gzip(
    source: Path,
    dest: Path,
    *,
    cancel_event: threading.Event | None,
) -> tuple[str, str, int]:
    """Fallback when the bgzip binary is absent. Correct, not fast -- measured
    at roughly 40 MB/s against bgzip's ~750 MB/s on real FASTQ, acceptable
    since a missing bgzip must degrade an ingest rather than fail it.

    `mtime=0` and `filename=""` keep the compressed bytes (and therefore their
    hash) deterministic for identical input. Without them, a gzip header
    embeds the current time and, since `fileobj` here is a real file rather
    than an in-memory buffer, the destination's basename too -- and `dest` is
    a fresh `mkstemp` name on every call, so two ingests of the same
    plaintext would hash differently for no reason related to their content.
    That would make dedup depend on when a file happened to be ingested and
    which random temp name it landed under, not on what is in it.
    """
    plain_hash = hashlib.sha256()
    compressed_hash = hashlib.sha256()
    compressed_size = 0
    since_check = 0

    with open(source, "rb") as f, open(dest, "wb") as raw_out:
        with gzip.GzipFile(
            fileobj=raw_out, filename="", mode="wb", compresslevel=COMPRESS_LEVEL, mtime=0
        ) as gz_out:
            while chunk := f.read(READ_BUFFER):
                plain_hash.update(chunk)
                since_check += len(chunk)
                if since_check >= CANCEL_CHECK_BYTES:
                    since_check = 0
                    if cancel_event is not None and cancel_event.is_set():
                        raise JobCancelled("Cancelled during compression")
                gz_out.write(chunk)
        raw_out.flush()
        os.fsync(raw_out.fileno())

    # Re-hash the compressed output: GzipFile buffers internally, so summing
    # chunks handed to `write` would not match what actually landed on disk.
    with open(dest, "rb") as f:
        while chunk := f.read(READ_BUFFER):
            compressed_hash.update(chunk)
            compressed_size += len(chunk)

    return plain_hash.hexdigest(), compressed_hash.hexdigest(), compressed_size
