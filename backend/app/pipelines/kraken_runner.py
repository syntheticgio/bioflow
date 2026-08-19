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


def parse_kraken_report(text: str) -> list[dict]:
    """Rows of Kraken2's six-column report.

    Columns: percentage of reads in the clade, clade read count, reads
    assigned directly to this taxon, rank code, NCBI taxid, and the name
    indented two spaces per tree level (stripped here -- the report is a
    flat fact source, not a tree render).

    Returns ``[]`` for anything unparseable rather than raising, the
    posture ``quast_runner.parse_report_tsv`` documents: a report that
    cannot be read must not fail a run that already produced real output.
    """
    rows: list[dict] = []
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        try:
            rows.append(
                {
                    "pct": float(fields[0]),
                    "clade_reads": int(fields[1]),
                    "direct_reads": int(fields[2]),
                    "rank": fields[3].strip(),
                    "taxid": int(fields[4]),
                    "name": fields[5].strip(),
                }
            )
        except ValueError:
            continue
    return rows


def parse_bracken_output(text: str) -> list[dict]:
    """Rows of Bracken's species table: name, taxid, abundance fraction.

    Same empty-on-garbage posture as ``parse_kraken_report``.
    """
    rows: list[dict] = []
    lines = text.splitlines()
    for line in lines[1:]:  # skip header
        fields = line.split("\t")
        if len(fields) != 7:
            continue
        try:
            rows.append(
                {
                    "name": fields[0].strip(),
                    "taxid": int(fields[1]),
                    "fraction": float(fields[6]),
                }
            )
        except ValueError:
            continue
    return rows
