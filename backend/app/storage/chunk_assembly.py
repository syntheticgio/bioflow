"""Chunk assembly: concatenate and hash in a single pass.

Reading a 100 GB file twice across VirtioFS costs roughly four extra minutes for
nothing. hashlib releases the GIL above ~2 KiB and file reads release it too, so
digesting while copying is effectively free -- the operation is bound by the
mount, not the CPU.

Synchronous by design; callers run it off the event loop.
"""

import hashlib
import os
import threading
from collections.abc import Callable
from pathlib import Path

from app.errors import JobCancelled, PermanentError
from app.logging import get_logger

log = get_logger(__name__)

READ_BUFFER = 4 * 1024 * 1024
CANCEL_CHECK_BYTES = 8 * 1024 * 1024
PROGRESS_INTERVAL_BYTES = 64 * 1024 * 1024


def assemble_and_hash(
    chunk_paths: list[Path],
    target: Path,
    *,
    cancel_event: threading.Event | None = None,
    progress_cb: Callable[[int], None] | None = None,
) -> tuple[str, int]:
    """Concatenate chunks into `target`, returning (sha256_hex, total_bytes).

    Idempotent: the target is truncated on open, never appended to. A retry
    after a crash therefore rebuilds from scratch rather than doubling the file,
    which is the failure this ordering exists to prevent.
    """
    for path in chunk_paths:
        if not path.exists():
            raise PermanentError(f"Chunk missing during assembly: {path.name}")

    target.parent.mkdir(parents=True, exist_ok=True)

    h = hashlib.sha256()
    written = 0
    since_cancel_check = 0
    since_progress = 0

    with open(target, "wb") as out:  # "wb" truncates -- see docstring
        for path in chunk_paths:
            with open(path, "rb") as src:
                while buf := src.read(READ_BUFFER):
                    out.write(buf)
                    h.update(buf)  # same pass, no second read
                    written += len(buf)
                    since_cancel_check += len(buf)
                    since_progress += len(buf)

                    if since_cancel_check >= CANCEL_CHECK_BYTES:
                        since_cancel_check = 0
                        if cancel_event is not None and cancel_event.is_set():
                            raise JobCancelled("Cancelled during assembly")

                    if progress_cb is not None and since_progress >= PROGRESS_INTERVAL_BYTES:
                        since_progress = 0
                        progress_cb(written)

        out.flush()
        os.fsync(out.fileno())

    if progress_cb is not None:
        progress_cb(written)

    return h.hexdigest(), written
