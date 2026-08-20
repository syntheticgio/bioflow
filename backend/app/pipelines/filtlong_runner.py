"""Building and observing a Filtlong run.

Kept separate from the job handler so the parts worth testing -- command
construction, report extraction -- are pure functions over strings and dicts,
with no queue or filesystem involved. Mirrors cutadapt_runner.py's shape.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from app.logging import get_logger

log = get_logger(__name__)

# Filtlong writes a key-value summary to stdout. Example:
#   Total reads:  12345
#   Total bases:  123456789
#   Reads kept:   10000 (81.0%)
#   Reads discarded:
#     Too short:  2000 (16.2%)
#     Too low quality: 345 (2.8%)
#   Bases kept:   120000000 (97.2%)
#   Bases discarded:
#     Too short:  3456789 (2.8%)
#     Too low quality: 0 (0.0%)
# A key-value summary line: "Total reads:  12345". Captures the key
# and the integer count, ignoring any trailing percentage in parentheses.
_SUMMARY_LINE = re.compile(r"^(\w[\w ]+):\s+(\d+)")

# A section header with no value, e.g. "Reads discarded:" -- Filtlong prints
# these as bare labels that introduce an indented per-reason breakdown below.
_SECTION_HEADER = re.compile(r"^(\w[\w ]+):\s*$")

# Indented lines under a discarded section carry the per-reason breakdown.
_INDENTED_LINE = re.compile(r"^\s{2,}(\w[\w ]+):\s+(\d+)")


@dataclass
class FiltlongParams:
    """User-facing knobs. Conservative defaults tuned for long reads:
    min_length=1000 filters reads shorter than 1 kb, min_mean_q=10 removes
    reads whose average Phred score is below 10, and keep_percent=90 keeps the
    best 90% of reads by quality."""

    min_length: int = 1000
    min_mean_q: int = 10
    keep_percent: int = 90
    target_bases: int | None = None
    threads: int = 4

    def as_dict(self) -> dict:
        return {
            "min_length": self.min_length,
            "min_mean_q": self.min_mean_q,
            "keep_percent": self.keep_percent,
            "target_bases": self.target_bases,
            "threads": self.threads,
        }

    @classmethod
    def from_dict(cls, raw: dict | None) -> "FiltlongParams":
        raw = raw or {}
        known = {f for f in cls().as_dict()}
        return cls(**{k: v for k, v in raw.items() if k in known and v is not None})


def build_command(
    *,
    filtlong_path: str,
    r1_in: Path,
    r1_out: Path,
    params: FiltlongParams,
    r2_in: Path | None = None,
    r2_out: Path | None = None,
) -> list[str]:
    """Assemble the filtlong invocation.

    Filtlong takes a single input FASTQ (or two for paired-end short reads
    used as a quality reference). The output goes to stdout unless -o is given.

    Unlike fastp/cutadapt, filtlong is single-end only for the actual
    filtering -- it processes one read stream. If a mate is provided, it is
    used as a short-read reference for quality weighting (--short_read1/2),
    not as a second read stream to filter.
    """
    cmd = [
        filtlong_path,
        "--min_length",
        str(params.min_length),
        "--min_mean_q",
        str(params.min_mean_q),
        "--keep_percent",
        str(params.keep_percent),
    ]

    if params.target_bases is not None:
        cmd += ["--target_bases", str(params.target_bases)]

    cmd += ["-o", str(r1_out), str(r1_in)]

    if r2_in is not None:
        # When a mate is given, it serves as a short-read reference for
        # quality scoring, not as a second read stream. Filtlong supports
        # --short_read1 and --short_read2 for paired short-read references.
        # r1_in is the long-read input being filtered (already added as the
        # positional argument above); r2_in is the short-read mate.
        cmd += ["--short_read1", str(r2_in)]

    return cmd


def parse_report(path: Path) -> dict:
    """Extract the before/after comparison from Filtlong's stdout summary.

    Filtlong writes a plain-text key-value summary to stdout (redirected to a
    file by the handler). Returns a dict shaped like the other trim tool
    reports so the UI can render it uniformly.
    """
    try:
        text = path.read_text()
    except OSError as e:
        log.warning("filtlong_report_unreadable", path=str(path), error=str(e))
        return {}

    lines = text.splitlines()

    summary: dict[str, int] = {}
    current_section: str | None = None
    discarded: dict[str, int] = {}

    for line in lines:
        match = _SUMMARY_LINE.match(line)
        if match:
            key = match.group(1).strip()
            summary[key] = int(match.group(2))
            # Only "Reads discarded" introduces per-reason read counts.
            # "Bases discarded" has its own Too short/Too low quality lines
            # that must not clobber the read counts already captured.
            current_section = "reads_discarded" if key == "Reads discarded" else None
            continue

        match = _SECTION_HEADER.match(line)
        if match:
            key = match.group(1).strip()
            current_section = "reads_discarded" if key == "Reads discarded" else None
            continue

        match = _INDENTED_LINE.match(line)
        if match and current_section:
            discarded[match.group(1).strip()] = int(match.group(2))

    total_in = summary.get("Total reads")
    reads_kept = summary.get("Reads kept")
    total_bases_in = summary.get("Total bases")
    bases_kept = summary.get("Bases kept")

    if total_in is None or reads_kept is None:
        log.warning("filtlong_report_missing_keys", path=str(path))
        return {}

    return {
        "tool": "filtlong",
        "before": {
            "total_reads": total_in,
            "total_bases": total_bases_in,
        },
        "after": {
            "total_reads": reads_kept,
            "total_bases": bases_kept,
        },
        "filtering": {
            "too_short_reads": discarded.get("Too short"),
            "too_low_quality_reads": discarded.get("Too low quality"),
        },
    }


def output_name(source_name: str) -> str:
    """Name for a filtered file, derived from its source. Uses a `.filtered`
    suffix to distinguish from the `.trimmed` suffix used by short-read tools,
    since the two operations are conceptually different."""
    name = source_name
    suffixes = ""
    for ext in (".gz", ".bz2", ".zst"):
        if name.lower().endswith(ext):
            suffixes = name[-len(ext) :] + suffixes
            name = name[: -len(ext)]
            break
    for ext in (".fastq", ".fq"):
        if name.lower().endswith(ext):
            suffixes = name[-len(ext) :] + suffixes
            name = name[: -len(ext)]
            break

    if not suffixes:
        suffixes = ".fastq.gz"
    return f"{name}.filtered{suffixes}"
