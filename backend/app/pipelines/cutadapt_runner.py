"""Building and observing a cutadapt run.

Kept separate from the job handler so the parts worth testing -- command
construction, report extraction -- are pure functions over strings and dicts,
with no queue or filesystem involved. Mirrors fastp_runner.py's shape.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from app.logging import get_logger

log = get_logger(__name__)


@dataclass
class CutadaptParams:
    """User-facing knobs. min_length defaults to 1, not 0: cutadapt keeps
    zero-length reads unless told otherwise, and downstream tools choke on
    them the same way an unset fastp --length_required would not, since
    fastp's own default there is already nonzero."""

    quality_cutoff: int = 20
    min_length: int = 1
    adapter_r1: str | None = None
    adapter_r2: str | None = None
    threads: int = 4

    def as_dict(self) -> dict:
        return {
            "quality_cutoff": self.quality_cutoff,
            "min_length": self.min_length,
            "adapter_r1": self.adapter_r1,
            "adapter_r2": self.adapter_r2,
            "threads": self.threads,
        }

    @classmethod
    def from_dict(cls, raw: dict | None) -> "CutadaptParams":
        raw = raw or {}
        known = {f for f in cls().as_dict()}
        return cls(**{k: v for k, v in raw.items() if k in known and v is not None})


def build_command(
    *,
    cutadapt_path: str,
    r1_in: Path,
    r1_out: Path,
    json_out: Path,
    params: CutadaptParams,
    r2_in: Path | None = None,
    r2_out: Path | None = None,
) -> list[str]:
    """Assemble the cutadapt invocation.

    Unlike fastp, cutadapt has no --verbose progress stream and no adapter
    auto-detection -- an adapter must be given explicitly or none is searched
    for, which is a real behavioral difference from fastp's
    detect_adapter_for_pe default, not an oversight here.
    """
    paired = r2_in is not None
    if paired and r2_out is None:
        raise ValueError("paired input requires a second output path")

    cmd = [
        cutadapt_path,
        f"--json={json_out}",
        "-j",
        str(params.threads),
        "-q",
        str(params.quality_cutoff),
        "-m",
        str(params.min_length),
    ]

    if params.adapter_r1:
        cmd += ["-a", params.adapter_r1]
    if paired and params.adapter_r2:
        cmd += ["-A", params.adapter_r2]

    cmd += ["-o", str(r1_out)]
    if paired:
        cmd += ["-p", str(r2_out)]

    cmd.append(str(r1_in))
    if paired:
        cmd.append(str(r2_in))

    return cmd


def parse_report(path: Path) -> dict:
    """Extract the before/after comparison from cutadapt's JSON.

    Only the scalar summary is kept, matching fastp_runner.parse_report --
    the full report also carries per-adapter trimmed-length histograms that
    belong in a future HTML/detail view, not in every object's facts.
    """
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        log.warning("cutadapt_report_unreadable", path=str(path), error=str(e))
        return {}

    reads = raw.get("read_counts", {})
    bases = raw.get("basepair_counts", {})
    filtered = reads.get("filtered", {})

    report = {
        "tool": "cutadapt",
        "tool_version": raw.get("cutadapt_version"),
        "before": {
            "total_reads": reads.get("input"),
            "total_bases": bases.get("input"),
        },
        "after": {
            "total_reads": reads.get("output"),
            "total_bases": bases.get("output"),
        },
        "filtering": {
            "too_short_reads": filtered.get("too_short"),
            "too_long_reads": filtered.get("too_long"),
        },
    }

    r1_adapter = reads.get("read1_with_adapter")
    r2_adapter = reads.get("read2_with_adapter")
    if r1_adapter is not None or r2_adapter is not None:
        report["adapters"] = {
            "trimmed_reads_r1": r1_adapter,
            "trimmed_reads_r2": r2_adapter,
        }

    return report


def output_name(source_name: str) -> str:
    """Name for a trimmed file, derived from its source. Identical rule to
    fastp_runner.output_name -- both tools produce the same kind of output,
    so the naming convention that keeps mate detection working is shared."""
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
    return f"{name}.trimmed{suffixes}"
