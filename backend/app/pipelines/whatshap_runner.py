"""Running WhatsHap to phase a called-variant VCF against its alignments.

Two subcommands matter here, both emitting a phased VCF carrying PS tags:

- `whatshap phase` phases a single sample against one BAM.
- `whatshap polyphase` phases multiple samples, one BAM each.

The commands are small; the tuning in them is not, and -- like the csq
runner -- the flag set is settled by running the tool against a real Clair3
callset rather than read from the manual. The constants below are the place
that tuning lands so it stays one named decision, not scattered argv.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Which subcommand to run.
PHASE = "phase"
POLYPHASE = "polyphase"

# whatshap writes progress and INFO to stderr on a normal run, and raises (a
# Python Traceback) or logs an ERROR only on failure. Defaulting an
# unrecognised line to *benign* (unlike the csq classifier, which surfaces
# unknowns as noise) is the right call for a progress-heavy tool: a
# successful run must not be read as failed because of its own logging.
# The run still fails on a non-zero exit, so a rephrased error is caught by
# the return code even if it slips past these prefixes. Refined against a
# real run for #628.
_WHATSHAP_ERROR_PREFIXES = ("[e::", "error", "critical", "traceback")


@dataclass
class WhatshapParams:
    """The tunable knobs that travel in a run's params.

    The BAM(s) are not here: for `phase` the single alignment comes from the
    node input / card picker, and for `polyphase` the per-sample BAMs are
    resolved from object ids at run time. Only the choices that survive a
    round-trip through JSON belong in this struct.
    """

    mode: str = PHASE
    threads: int = 4
    sample: str | None = None
    ignore_read_groups: bool = False
    distrust_genotypes: bool = False
    indels: bool = False

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "threads": self.threads,
            "sample": self.sample,
            "ignore_read_groups": self.ignore_read_groups,
            "distrust_genotypes": self.distrust_genotypes,
            "indels": self.indels,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> WhatshapParams:
        data = data or {}
        mode = data.get("mode", PHASE)
        if mode not in (PHASE, POLYPHASE):
            raise ValueError(f"unknown whatshap mode: {mode!r}")
        return cls(
            mode=mode,
            threads=int(data.get("threads", 4)),
            sample=data.get("sample"),
            ignore_read_groups=bool(data.get("ignore_read_groups", False)),
            distrust_genotypes=bool(data.get("distrust_genotypes", False)),
            indels=bool(data.get("indels", False)),
        )


def build_whatshap_phase_command(
    *,
    whatshap_path: str,
    reference: Path,
    bam: Path,
    vcf: Path,
    out: Path,
    threads: int = 4,
    sample: str | None = None,
    ignore_read_groups: bool = False,
    distrust_genotypes: bool = False,
    indels: bool = False,
) -> list[str]:
    """`whatshap phase` over one VCF and one BAM, writing a bgzipped VCF.

    VCF precedes BAM: that is whatsHap's positional order. `--sample` is
    optional and only meaningful for a multi-sample VCF being phased as one.
    """
    cmd = [
        whatshap_path,
        "phase",
        "--reference",
        str(reference),
        "--output",
        str(out),
        "--threads",
        str(threads),
    ]
    if sample:
        cmd += ["--sample", sample]
    if ignore_read_groups:
        cmd.append("--ignore-read-groups")
    if distrust_genotypes:
        cmd.append("--distrust-genotypes")
    if indels:
        cmd.append("--indels")
    cmd += [str(vcf), str(bam)]
    return cmd


def build_whatshap_polyphase_command(
    *,
    whatshap_path: str,
    reference: Path,
    samples: dict[str, Path],
    vcf: Path,
    out: Path,
    threads: int = 4,
    ignore_read_groups: bool = False,
    distrust_genotypes: bool = False,
    indels: bool = False,
) -> list[str]:
    """`whatshap polyphase` over one VCF and one BAM per sample.

    Each `--sample NAME BAM` maps a sample to its alignment; whatsHap
    requires the sample names to match the VCF's. The VCF is the only
    positional argument and comes last.
    """
    if not samples:
        raise ValueError("polyphase requires at least one sample BAM")
    cmd = [
        whatshap_path,
        "polyphase",
        "--reference",
        str(reference),
        "--output",
        str(out),
        "--threads",
        str(threads),
    ]
    for name, bam in samples.items():
        cmd += ["--sample", name, str(bam)]
    if ignore_read_groups:
        cmd.append("--ignore-read-groups")
    if distrust_genotypes:
        cmd.append("--distrust-genotypes")
    if indels:
        cmd.append("--indels")
    cmd.append(str(vcf))
    return cmd


def is_benign_whatshap_stderr(line: str) -> bool:
    """Whether a stderr line is ordinary whatsHap progress rather than failure.

    Mirrors the csq runner's split -- explicit error prefixes first, then a
    default -- but inverts the default: csq surfaces unknowns as noise because
    a swallowed GFF warning is cheap, whereas whatsHap's normal run is mostly
    progress logging to stderr and must not be mistaken for an error. A real
    failure exits non-zero, so it is caught by the return code regardless.
    """
    stripped = line.lstrip().lower()
    if any(stripped.startswith(prefix) for prefix in _WHATSHAP_ERROR_PREFIXES):
        return False
    return True


# Suffixes a VCF may arrive with, longest first so `.vcf.gz` is stripped whole
# rather than leaving a stray `.vcf`.
_VCF_SUFFIXES = (".vcf.gz", ".vcf", ".bcf")


def phased_name(vcf_name: str, mode: str = PHASE) -> str:
    """The output name for a phased copy of `vcf_name`.

    Mirrors csq_runner.annotated_name: `.vcf.gz` has a double extension, so
    the stem keeps the inner `.vcf`. phase yields `foo.bcftools.phase.vcf.gz`,
    polyphase yields `foo.bcftools.polyphase.vcf.gz`.
    """
    tag = ".polyphase" if mode == POLYPHASE else ".phase"
    for suffix in _VCF_SUFFIXES:
        if vcf_name.endswith(suffix):
            return f"{vcf_name[: -len(suffix)]}{tag}.vcf.gz"
    return f"{vcf_name}{tag}.vcf.gz"
