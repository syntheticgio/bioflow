"""Building and observing a Trimmomatic run.

Trimmomatic ships as two separate binaries -- TrimmomaticPE and
TrimmomaticSE -- around one JAR, with no combined entry point and no JSON
report; both differences from fastp/cutadapt are real, not oversights, and
drive the shape below. It does have a `-summary <file>` flag, though --
confirmed against a real run during planning -- so parse_summary reads that
file rather than regexing stdout.
"""

from dataclasses import dataclass
from pathlib import Path

from app.logging import get_logger

log = get_logger(__name__)

# Key names as they appear in a -summary file, mapped to where each value
# lands in the report dict. Confirmed against a real TrimmomaticSE/PE run
# during planning -- see the module's test file for the exact byte-for-byte
# fixtures. PE and SE use different key names for the same concept ("Both
# Surviving Reads" vs "Surviving Reads"), so both are listed; whichever is
# present for the given `paired` value is read.
_SE_KEYS = {"input": "Input Reads", "surviving": "Surviving Reads", "dropped": "Dropped Reads"}
_PE_KEYS = {
    "input": "Input Read Pairs",
    "surviving": "Both Surviving Reads",
    "dropped": "Dropped Reads",
}


@dataclass
class TrimmomaticParams:
    """User-facing knobs. adapter_file names a FASTA under
    settings.trimmomatic_adapters_dir -- TruSeq3 is the modern HiSeq/MiSeq
    adapter set and Trimmomatic's own quick-start default."""

    quality_leading: int = 3
    quality_trailing: int = 3
    sliding_window_size: int = 4
    sliding_window_quality: int = 15
    min_length: int = 36  # Trimmomatic's own documented default
    adapter_file: str | None = "TruSeq3-SE.fa"
    threads: int = 4

    def as_dict(self) -> dict:
        return {
            "quality_leading": self.quality_leading,
            "quality_trailing": self.quality_trailing,
            "sliding_window_size": self.sliding_window_size,
            "sliding_window_quality": self.sliding_window_quality,
            "min_length": self.min_length,
            "adapter_file": self.adapter_file,
            "threads": self.threads,
        }

    @classmethod
    def from_dict(cls, raw: dict | None) -> "TrimmomaticParams":
        raw = raw or {}
        known = {f for f in cls().as_dict()}
        return cls(**{k: v for k, v in raw.items() if k in known and v is not None})


def build_command(
    *,
    trimmomatic_pe_path: str,
    trimmomatic_se_path: str,
    adapters_dir: str,
    r1_in: Path,
    r1_out: Path,
    summary_out: Path,
    params: TrimmomaticParams,
    r2_in: Path | None = None,
    r2_out: Path | None = None,
    unpaired_r1_out: Path | None = None,
    unpaired_r2_out: Path | None = None,
) -> list[str]:
    """Assemble the TrimmomaticPE/SE invocation.

    Picks the binary and the default adapter file by read layout: a PE
    adapter file used on single-end reads (or vice versa) matches nothing and
    silently does no clipping, which is worse than an error -- so the default
    tracks `paired` rather than being one fixed filename.

    -phred33 is always passed rather than left to autodetection: Trimmomatic
    fails outright ("Unable to detect quality encoding") on inputs too short
    for it to guess from, confirmed against a real run during planning, and
    every FASTQ this application handles is phred+33 -- autodetection buys
    nothing and adds a failure mode. -summary writes the file parse_summary
    reads; there is no flag to send it to stdout instead.
    """
    paired = r2_in is not None
    if paired and (unpaired_r1_out is None or unpaired_r2_out is None):
        raise ValueError("paired input requires both unpaired output paths")

    adapter_file = params.adapter_file
    if paired and adapter_file == "TruSeq3-SE.fa":
        adapter_file = "TruSeq3-PE.fa"

    if paired:
        cmd = [
            trimmomatic_pe_path,
            "-threads",
            str(params.threads),
            "-phred33",
            "-summary",
            str(summary_out),
        ]
        cmd += [str(r1_in), str(r2_in)]
        cmd += [str(r1_out), str(unpaired_r1_out), str(r2_out), str(unpaired_r2_out)]
    else:
        cmd = [
            trimmomatic_se_path,
            "-threads",
            str(params.threads),
            "-phred33",
            "-summary",
            str(summary_out),
        ]
        cmd += [str(r1_in), str(r1_out)]

    if adapter_file:
        cmd.append(
            f"ILLUMINACLIP:{adapters_dir.rstrip('/')}/{adapter_file}:2:30:10"
        )

    cmd += [
        f"LEADING:{params.quality_leading}",
        f"TRAILING:{params.quality_trailing}",
        f"SLIDINGWINDOW:{params.sliding_window_size}:{params.sliding_window_quality}",
        f"MINLEN:{params.min_length}",
    ]

    return cmd


def parse_summary(path: Path, *, paired: bool) -> dict:
    """Extract read counts from a Trimmomatic `-summary` file.

    The file is `Key: Value` per line. PE and SE name the survival count
    differently ("Both Surviving Reads" vs "Surviving Reads") for the same
    concept, so which keys to read is chosen by `paired` -- matching how the
    handler already knows its own read layout rather than sniffing the file.
    """
    try:
        lines = path.read_text().splitlines()
    except OSError as e:
        log.warning("trimmomatic_summary_unreadable", path=str(path), error=str(e))
        return {}

    values: dict[str, int] = {}
    for line in lines:
        if ":" not in line:
            continue
        key, _, raw_value = line.partition(":")
        try:
            values[key.strip()] = int(raw_value.strip())
        except ValueError:
            continue  # a *_Percent line, or anything else non-integer

    keys = _PE_KEYS if paired else _SE_KEYS
    total_in = values.get(keys["input"])
    total_out = values.get(keys["surviving"])
    dropped = values.get(keys["dropped"])

    if total_in is None or total_out is None or dropped is None:
        log.warning("trimmomatic_summary_missing_keys", path=str(path), paired=paired)
        return {}

    return {
        "tool": "trimmomatic",
        "before": {"total_reads": total_in},
        "after": {"total_reads": total_out},
        "filtering": {"dropped_reads": dropped},
    }


def output_name(source_name: str) -> str:
    """Identical rule to fastp_runner.output_name and cutadapt_runner.output_name."""
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
