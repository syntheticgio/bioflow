"""iVar command construction and consensus stderr parsing.

Same split `completeness_runner` and `assembly_runner` use: pure functions
over strings and paths, testable without a container, a queue, or a binary.

Command shapes and output format verified against a real installed 1.4.4
binary on 2026-08-05: synthetic reads aligned against a small reference,
trimmed with a real primer BED, sorted, and piped through
`samtools mpileup -A -d 0 -Q 0` into `ivar consensus`. Two findings that
shaped this module:

- `ivar trim -p trimmed` writes `trimmed.bam`, unsorted. `ivar consensus`
  reads a pileup, which needs position-sorted input, so a sort stage sits
  between trim and consensus -- omitting it is the failure mode the design
  calls out as easy to miss and confusing to debug.
- iVar's own consensus summary (reference length, positions at zero depth,
  positions below the depth floor) goes to stderr, not a file.
"""

import re
import shlex
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ConsensusParams:
    # iVar's own default (`-q 20`), passed explicitly rather than omitted:
    # the design records these as facts on the output object, so the
    # runner must never let iVar's own defaults apply silently.
    min_quality: int = 20
    # iVar's own default (`-t 0`, majority/most-common base).
    min_freq: float = 0.0
    # iVar's own default (`-m 10`).
    min_depth: int = 10


def build_trim_command(
    *, ivar_path: str, bam: Path, primer_bed: Path, out_prefix: Path
) -> list[str]:
    """The argv for one `ivar trim` run. Output is `<out_prefix>.bam`,
    unsorted -- see module docstring."""
    return [
        ivar_path,
        "trim",
        "-i",
        str(bam),
        "-b",
        str(primer_bed),
        "-p",
        str(out_prefix),
    ]


def build_sort_command(*, samtools_path: str, bam: Path, out: Path) -> list[str]:
    """The argv for sorting a trimmed BAM before it can be piled up."""
    return [samtools_path, "sort", "-o", str(out), str(bam)]


def _quote(argv: list[str]) -> str:
    """Shell-quote an argv for embedding in a `sh -c` string.

    Same helper `align_runner._quote` provides for `aligner | samtools
    sort` -- every element goes through shlex.quote because filenames come
    from user-supplied object names, not from a fixed template.
    """
    return " ".join(shlex.quote(a) for a in argv)


def build_consensus_command(
    *,
    samtools_path: str,
    ivar_path: str,
    bam: Path,
    reference: Path,
    out_prefix: Path,
    params: ConsensusParams,
) -> list[str]:
    """`samtools mpileup | ivar consensus`, as a `/bin/sh -o pipefail`
    invocation.

    Same shape and same reasoning as `align_runner.build_align_command`'s
    `aligner | samtools sort`: the exit status of a shell pipe is the
    *last* command's, so an unwrapped pipe would report `ivar consensus`'s
    own success even when `mpileup` failed and it consumed nothing.
    `pipefail` is what makes a failing first stage fail the job. `sh`, not
    `bash`: the base image has no bash, and `-o pipefail` is supported by
    Debian's dash as of the version trixie ships.

    The mpileup flags are fixed, not parameterized, because each disables a
    samtools default that is wrong for amplicon data: `-A` keeps anomalous
    read pairs (normal at amplicon boundaries), `-d 0` removes the depth
    cap (amplicon data is routinely far above the default cap), and `-Q 0`
    disables samtools' own base-quality filter so iVar's `-q` is the only
    one actually in effect.
    """
    mpileup_argv = [
        samtools_path,
        "mpileup",
        "-A",
        "-d",
        "0",
        "-Q",
        "0",
        "--reference",
        str(reference),
        str(bam),
    ]
    consensus_argv = [
        ivar_path,
        "consensus",
        "-p",
        str(out_prefix),
        "-q",
        str(params.min_quality),
        "-t",
        str(params.min_freq),
        "-m",
        str(params.min_depth),
    ]
    pipeline = f"{_quote(mpileup_argv)} | {_quote(consensus_argv)}"
    return ["/bin/sh", "-o", "pipefail", "-c", pipeline]


_REFERENCE_LENGTH_RE = re.compile(r"^Reference length:\s*(\d+)\s*$", re.MULTILINE)
_ZERO_DEPTH_RE = re.compile(r"^Positions with 0 depth:\s*(\d+)\s*$", re.MULTILINE)
_LOW_DEPTH_RE = re.compile(
    r"^Positions with depth below \d+:\s*(\d+)\s*$", re.MULTILINE
)


def parse_consensus_stderr(text: str) -> dict:
    """`consensus_*` facts from iVar's own stderr summary.

    Returns {} for anything unparseable rather than raising -- the same
    posture `completeness_runner.parse_summary` takes: a summary that failed
    to parse must not fail a job that already produced a consensus FASTA.
    """
    ref_length = _REFERENCE_LENGTH_RE.search(text)
    zero_depth = _ZERO_DEPTH_RE.search(text)
    low_depth = _LOW_DEPTH_RE.search(text)

    if ref_length is None:
        return {}

    facts: dict = {"consensus_reference_length": int(ref_length.group(1))}
    if zero_depth is not None:
        facts["consensus_zero_depth_positions"] = int(zero_depth.group(1))
    if low_depth is not None:
        facts["consensus_low_depth_positions"] = int(low_depth.group(1))
    return facts
