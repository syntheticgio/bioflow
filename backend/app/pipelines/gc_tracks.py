"""Per-window GC content and GC skew for a multi-contig assembly.

A pure, streaming, cancellable scanner — not a sampler. The existing
`sequence_stats.fasta_stats` computes whole-assembly GC from ~2,000
disjoint blocks and cannot produce contiguous ordered windows. GC skew
is cumulative and order-dependent, so nothing but a full scan works.

Strategy: stream the FASTA, buffer one contig at a time, then resolve
windows from the buffer when the contig ends.  Peak memory is the
largest contig, which for a human genome is ~250 MB (chr1) — comfortably
inside the handler's 2 GB budget.
"""

import threading
from pathlib import Path

from app.errors import JobCancelled
from app.logging import get_logger
from app.models import Compression

# Reused rather than duplicated: the existing cap on stored contigs
# is exactly the right number — a draft with thousands of contigs has
# no meaningful circular representation.
from app.storage.parsers import MAX_STORED_CONTIGS  # noqa: E402

log = get_logger(__name__)

WINDOW_COUNT = 500
MIN_WINDOW_BASES = 100


def compute_gc_tracks(
    path: Path,
    compression: Compression,
    *,
    cancel_event: threading.Event | None = None,
) -> dict:
    """Scan a FASTA and return per-contig, per-window GC and skew tracks.

    Returns {} on an unreadable file (matching fasta_stats) and re-raises
    JobCancelled.  Window width is derived per contig from WINDOW_COUNT,
    floored at MIN_WINDOW_BASES.
    """
    import gzip

    contigs: list[tuple[str, int, list[str]]] = []
    current_name: str | None = None
    current_buf: list[str] = []
    chars_scanned = 0

    def _check_cancel():
        if cancel_event is not None and cancel_event.is_set():
            raise JobCancelled("gc_tracks cancelled")

    is_compressed = compression in (Compression.GZIP, Compression.BGZF)

    try:
        opener = gzip.open if is_compressed else open
        with opener(path, "rt", errors="replace") as fh:
            for line in fh:
                stripped = line.rstrip("\n\r")
                chars_scanned += len(stripped)
                if chars_scanned >= 1_000_000:
                    _check_cancel()
                    chars_scanned = 0

                if stripped.startswith(">"):
                    # Commit previous contig
                    if current_name is not None:
                        contigs.append((current_name, len(current_buf), current_buf))
                    current_name = stripped[1:].split()[0]
                    current_buf = []
                elif current_name is not None:
                    current_buf.append(stripped)

        if current_name is not None:
            contigs.append((current_name, len(current_buf), current_buf))
    except JobCancelled:
        raise
    except (OSError, EOFError, UnicodeDecodeError) as e:
        log.warning("gc_tracks_scan_failed", path=str(path), error=str(e))
        return {}

    if not contigs:
        return {}

    # ── resolve per contig ───────────────────────────────────────────

    resolved = []
    for name, buf_len, buf in contigs:
        # Reconstruct full sequence length from the buffered lines
        total_length = sum(len(line) for line in buf) if buf else buf_len
        window_count = min(WINDOW_COUNT, total_length // MIN_WINDOW_BASES)
        if window_count == 0:
            continue
        window_bases = total_length // window_count
        if window_bases == 0:
            continue

        # Flatten buffer into a single string for windowed counting.
        # buf is list[str] — joining is O(total_length).
        seq = "".join(buf)

        gc_list: list[float | None] = []
        skew_list: list[float | None] = []

        for wi in range(window_count):
            start = wi * window_bases
            end = start + window_bases
            chunk = seq[start:end] if end <= total_length else seq[start:]

            g = chunk.count("G") + chunk.count("g")
            c = chunk.count("C") + chunk.count("c")
            a = chunk.count("A") + chunk.count("a")
            t = chunk.count("T") + chunk.count("t")
            acgt = g + c + a + t

            if acgt == 0:
                gc_list.append(None)
                skew_list.append(None)
            else:
                gc_list.append(round(100.0 * (g + c) / acgt, 2))
                skew_val = (g - c) / (g + c) if (g + c) > 0 else 0.0
                skew_list.append(round(skew_val, 2))

        resolved.append({
            "name": name,
            "length": total_length,
            "window_bases": window_bases,
            "gc": gc_list,
            "skew": skew_list,
        })

    # ── keep longest ─────────────────────────────────────────────────

    resolved.sort(key=lambda c: c["length"], reverse=True)
    partial = len(resolved) > MAX_STORED_CONTIGS
    if partial:
        resolved = resolved[:MAX_STORED_CONTIGS]

    result: dict = {
        "window_count": WINDOW_COUNT,
        "contigs": resolved,
    }
    if partial:
        result["gc_tracks_partial"] = True
    return result
