"""Kraken2/Bracken command construction and report parsing.

Same split ``quast_runner`` and ``bakta_runner`` use: pure functions over
strings and paths, testable without a container, a queue, or a binary.
"""

from __future__ import annotations

from pathlib import Path


def build_kraken2_command(
    *,
    kraken2_path: str,
    db_dir: Path,
    reads: Path,
    mate: Path | None,
    report: Path,
    output: Path,
    threads: int,
    gzipped: bool,
) -> list[str]:
    """The argv for ``kraken2`` over one read set.

    ``--memory-mapping`` is deliberately absent: it trades a full in-RAM
    load for page-in on every access, which is the slow path.  Memory is
    budgeted honestly instead, via the registry's per-database ``mem_mb``
    (spec K2-C3).  ``output`` is normally /dev/null -- the per-read
    assignments are enormous and nothing here consumes them; the report is
    the deliverable.
    """
    cmd = [
        kraken2_path,
        "--db", str(db_dir),
        "--threads", str(threads),
        "--report", str(report),
        "--output", str(output),
    ]
    if gzipped:
        cmd.append("--gzip-compressed")
    if mate is not None:
        cmd.append("--paired")
        cmd.extend([str(reads), str(mate)])
    else:
        cmd.append(str(reads))
    return cmd


def build_bracken_command(
    *,
    bracken_path: str,
    db_dir: Path,
    report: Path,
    output: Path,
    read_len: int,
) -> list[str]:
    """The argv for ``bracken`` over an existing Kraken2 report.

    ``-l S``: species-level re-estimation, the rank the taxonomy fact and
    the mismatch check consume.  ``read_len`` comes from the reads object's
    stored stats, defaulting to 100 (spec K2-R3) -- Bracken only accepts
    lengths its database distribution was built for, and the pre-built
    databases ship distributions for 50..300 in steps of 50, so the caller
    rounds to the nearest of those.
    """
    return [
        bracken_path,
        "-d", str(db_dir),
        "-i", str(report),
        "-o", str(output),
        "-r", str(read_len),
        "-l", "S",
    ]
