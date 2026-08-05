"""Building and observing a fastp run.

Kept separate from the job handler so the parts worth testing -- command
construction, progress parsing, report extraction -- are pure functions over
strings and dicts, with no queue or filesystem involved.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.logging import get_logger

log = get_logger(__name__)

# fastp --verbose emits one of these per million reads *loaded*:
#   [12:13:51] Read1: loaded 3M reads
# Loading runs ahead of processing, so this is an upper bound on real progress
# -- see TrimProgress.
_LOADED_RE = re.compile(r"Read([12]):\s*loaded\s+(\d+)M\s+reads", re.IGNORECASE)

# Coarse phase markers, in the order fastp reaches them.
_PHASES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"start to load data", re.IGNORECASE), "loading"),
    (re.compile(r"loading completed", re.IGNORECASE), "trimming"),
    (re.compile(r"data processing completed", re.IGNORECASE), "trimming"),
    (re.compile(r"writer finished", re.IGNORECASE), "writing"),
    (re.compile(r"start to generate reports", re.IGNORECASE), "reporting"),
)

# The distinct phase names from _PHASES above, in order, with duplicates
# collapsed ("trimming" is reached by two different log lines). fastp's own
# stage list is closed and known ahead of time -- unlike Flye's, which is why
# assembly_runner has no equivalent -- so a fixed ordered list is honest here.
PHASE_ORDER: tuple[str, ...] = ("loading", "trimming", "writing", "reporting")

# Progress derived from a read count is never allowed to reach 100%: the count
# is an estimate extrapolated from the first 1000 records, and a bar that sits
# at 100% while the job is still running is worse than one that sits at 95%.
MAX_MEASURED_PCT = 0.95


@dataclass
class TrimParams:
    """User-facing knobs. Defaults match fastp's own, except where this
    application has an opinion."""

    quality_threshold: int = 15  # fastp default
    unqualified_percent_limit: int = 40
    min_length: int = 15
    trim_poly_g: bool | None = None  # None = let fastp auto-detect NovaSeq/NextSeq
    trim_poly_x: bool = False
    dedup: bool = False
    detect_adapter_for_pe: bool = True
    adapter_r1: str | None = None
    adapter_r2: str | None = None
    threads: int = 4
    compression: int = 4  # fastp default; higher is smaller but slower

    def as_dict(self) -> dict:
        return {
            "quality_threshold": self.quality_threshold,
            "unqualified_percent_limit": self.unqualified_percent_limit,
            "min_length": self.min_length,
            "trim_poly_g": self.trim_poly_g,
            "trim_poly_x": self.trim_poly_x,
            "dedup": self.dedup,
            "detect_adapter_for_pe": self.detect_adapter_for_pe,
            "adapter_r1": self.adapter_r1,
            "adapter_r2": self.adapter_r2,
            "threads": self.threads,
            "compression": self.compression,
        }

    @classmethod
    def from_dict(cls, raw: dict | None) -> "TrimParams":
        raw = raw or {}
        known = {f for f in cls().as_dict()}
        return cls(**{k: v for k, v in raw.items() if k in known and v is not None})


def build_command(
    *,
    fastp_path: str,
    r1_in: Path,
    r1_out: Path,
    json_out: Path,
    html_out: Path,
    params: TrimParams,
    r2_in: Path | None = None,
    r2_out: Path | None = None,
) -> list[str]:
    """Assemble the fastp invocation.

    --verbose is not optional here: without it fastp prints nothing until the
    run is over, so there is no way to report progress on a job that may take
    hours.
    """
    paired = r2_in is not None
    if paired and r2_out is None:
        raise ValueError("paired input requires a second output path")

    cmd = [
        fastp_path,
        "--verbose",
        "-i",
        str(r1_in),
        "-o",
        str(r1_out),
    ]
    if paired:
        cmd += ["-I", str(r2_in), "-O", str(r2_out)]

    cmd += [
        "--json",
        str(json_out),
        "--html",
        str(html_out),
        "--thread",
        str(params.threads),
        "--compression",
        str(params.compression),
        "--qualified_quality_phred",
        str(params.quality_threshold),
        "--unqualified_percent_limit",
        str(params.unqualified_percent_limit),
        "--length_required",
        str(params.min_length),
    ]

    # Adapters: an explicit sequence always wins over detection. For paired
    # input fastp finds adapters by overlap analysis, which is more reliable
    # than matching a known list, so detection is worth asking for explicitly.
    if params.adapter_r1:
        cmd += ["--adapter_sequence", params.adapter_r1]
    if params.adapter_r2 and paired:
        cmd += ["--adapter_sequence_r2", params.adapter_r2]
    if paired and params.detect_adapter_for_pe and not params.adapter_r1:
        cmd += ["--detect_adapter_for_pe"]

    # polyG trimming defaults to on for two-colour chemistry, which fastp
    # detects from the instrument. Only override when the user insists.
    if params.trim_poly_g is True:
        cmd += ["--trim_poly_g"]
    elif params.trim_poly_g is False:
        cmd += ["--disable_trim_poly_g"]

    if params.trim_poly_x:
        cmd += ["--trim_poly_x"]
    if params.dedup:
        cmd += ["--dedup"]

    return cmd


def build_qc_command(
    *,
    fastp_path: str,
    r1_in: Path,
    json_out: Path,
    html_out: Path,
    r2_in: Path | None = None,
    threads: int = 4,
) -> list[str]:
    """Assemble a report-only fastp invocation.

    QC is read-only: it inspects a file and reports on it, where trimming
    derives new ones. Passing no `-o`/`-O` is what makes fastp skip writing
    reads -- there is no dedicated flag -- so this deliberately builds a
    command that `build_command` cannot express rather than threading an
    `output=None` special case through it.

    None of the filtering knobs are passed either. Their defaults would be
    applied to the *reported* numbers, so a file would appear to have fewer
    reads than it has; the point of QC is to describe the file as it is.
    """
    cmd = [fastp_path, "--verbose", "-i", str(r1_in)]
    if r2_in is not None:
        cmd += ["-I", str(r2_in)]
    cmd += [
        "--json",
        str(json_out),
        "--html",
        str(html_out),
        "--thread",
        str(threads),
        # Disables every filter, so the report describes the input rather than
        # what would survive a trim with default settings.
        "--disable_quality_filtering",
        "--disable_length_filtering",
        "--disable_adapter_trimming",
    ]
    return cmd


def parse_qc_facts(path: Path) -> dict:
    """QC facts for an object, from fastp's report-only JSON.

    Flatter than `parse_report`: with filtering disabled there is no
    before/after to compare, so the single measured state is reported directly
    under the `qc_` prefix the detail panel keys on.
    """
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        log.warning("fastp_qc_report_unreadable", path=str(path), error=str(e))
        return {}

    summary = raw.get("summary", {})
    before = summary.get("before_filtering", {})

    facts = {
        "qc_tool": "fastp",
        "qc_tool_version": summary.get("fastp_version"),
        "qc_sequencing": summary.get("sequencing"),
        "qc_before_filtering": _side(before),
        "qc_duplication_rate": raw.get("duplication", {}).get("rate"),
        "qc_insert_size_peak": raw.get("insert_size", {}).get("peak"),
    }

    adapters = raw.get("adapter_cutting", {})
    if adapters:
        facts["qc_adapters"] = {
            "read1_sequence": _adapter_or_none(adapters.get("read1_adapter_sequence")),
            "read2_sequence": _adapter_or_none(adapters.get("read2_adapter_sequence")),
        }

    return {k: v for k, v in facts.items() if v is not None}


@dataclass
class TrimProgress:
    """Turns fastp's verbose output into a progress fraction.

    The read total is an estimate (extrapolated from the first 1000 records at
    ingest), and fastp counts reads *loaded* rather than processed, so the
    result is an upper bound on an approximation. It is therefore capped below
    100% and reported as a phase plus a bar, never as a precise figure.
    """

    expected_reads: int | None = None
    phase: str = "starting"
    reads_loaded: int = 0
    _seen: dict[str, int] = field(default_factory=dict)

    def feed(self, line: str) -> bool:
        """Consume one output line. True if the caller should report progress."""
        changed = False

        for pattern, phase in _PHASES:
            if pattern.search(line):
                if self.phase != phase:
                    self.phase = phase
                    changed = True
                break

        match = _LOADED_RE.search(line)
        if match:
            mate, millions = match.group(1), int(match.group(2))
            # R1 and R2 are loaded concurrently and report independently; take
            # the furthest along rather than summing, which would double-count.
            self._seen[mate] = millions * 1_000_000
            loaded = max(self._seen.values())
            if loaded > self.reads_loaded:
                self.reads_loaded = loaded
                changed = True

        return changed

    @property
    def pct(self) -> float | None:
        """Fraction complete, or None when there is nothing honest to report."""
        if not self.expected_reads or self.reads_loaded <= 0:
            return None
        return min(self.reads_loaded / self.expected_reads, MAX_MEASURED_PCT)

    @property
    def phase_index(self) -> int | None:
        """Position in PHASE_ORDER, 1-based for "step N of M" display."""
        if self.phase not in PHASE_ORDER:
            return None
        return PHASE_ORDER.index(self.phase) + 1

    def message(self) -> str:
        if self.reads_loaded <= 0:
            return self.phase
        millions = self.reads_loaded / 1_000_000
        return f"{self.phase}: {millions:.0f}M reads read"


def parse_report(path: Path) -> dict:
    """Extract the before/after comparison from fastp's JSON.

    Only the scalar summary is kept. The full report also carries per-cycle
    quality and content curves -- several hundred floats per read direction --
    which belong in the HTML report rather than in every object document.
    """
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        log.warning("fastp_report_unreadable", path=str(path), error=str(e))
        return {}

    summary = raw.get("summary", {})
    before = summary.get("before_filtering", {})
    after = summary.get("after_filtering", {})
    filtering = raw.get("filtering_result", {})
    adapters = raw.get("adapter_cutting", {})

    report = {
        "tool": "fastp",
        "tool_version": summary.get("fastp_version"),
        "sequencing": summary.get("sequencing"),
        "before": _side(before),
        "after": _side(after),
        "filtering": {
            "passed_reads": filtering.get("passed_filter_reads"),
            "low_quality_reads": filtering.get("low_quality_reads"),
            "too_many_n_reads": filtering.get("too_many_N_reads"),
            "too_short_reads": filtering.get("too_short_reads"),
        },
        "duplication_rate": raw.get("duplication", {}).get("rate"),
        "insert_size_peak": raw.get("insert_size", {}).get("peak"),
    }

    if adapters:
        report["adapters"] = {
            "trimmed_reads": adapters.get("adapter_trimmed_reads"),
            "trimmed_bases": adapters.get("adapter_trimmed_bases"),
            "read1_sequence": _adapter_or_none(adapters.get("read1_adapter_sequence")),
            "read2_sequence": _adapter_or_none(adapters.get("read2_adapter_sequence")),
        }

    return report


def _side(block: dict) -> dict:
    return {
        "total_reads": block.get("total_reads"),
        "total_bases": block.get("total_bases"),
        "q20_rate": block.get("q20_rate"),
        "q30_rate": block.get("q30_rate"),
        "gc_content": block.get("gc_content"),
        "read1_mean_length": block.get("read1_mean_length"),
        "read2_mean_length": block.get("read2_mean_length"),
    }


def _adapter_or_none(value: str | None) -> str | None:
    """fastp writes the literal string 'unspecified' when it found nothing."""
    if not value or value.lower() == "unspecified":
        return None
    return value


def output_name(source_name: str) -> str:
    """Name for a trimmed file, derived from its source.

    `sample_R1.fastq.gz` becomes `sample_R1.trimmed.fastq.gz`: the marker goes
    before the format suffixes so the name still reads as a gzipped FASTQ, and
    the R1/R2 token is preserved so mate detection still works on the output.
    """
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
