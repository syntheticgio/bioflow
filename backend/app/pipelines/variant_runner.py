"""Building and observing a variant calling run.

Kept separate from the job handler so the parts worth testing -- command
construction, caller selection, progress parsing -- are pure functions over
strings and paths, with no queue or filesystem involved. Mirrors
`align_runner.py`, which splits the same way for the same reason.
"""

import re
import shlex
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from app.config import settings
from app.errors import PermanentError, ValidationError
from app.logging import get_logger
from app.pipelines.align_runner import ReadChemistry

log = get_logger(__name__)


class VariantCaller(StrEnum):
    CLAIR3 = "clair3"
    BCFTOOLS = "bcftools"
    # Recognized so the API can reject it with an explanation rather than
    # "unknown caller". Not installed: see _run_clair3's sibling check.
    DEEPVARIANT = "deepvariant"


@dataclass
class Clair3Params:
    """Clair3 invocation knobs."""

    threads: int = 4
    platform: str = "ont"  # {ont, hifi}

    def as_dict(self) -> dict:
        return {"threads": self.threads, "platform": self.platform}

    @classmethod
    def from_dict(cls, raw: dict | None) -> "Clair3Params":
        raw = raw or {}
        return cls(
            threads=int(raw.get("threads", 4)),
            platform=str(raw.get("platform", "ont")),
        )


@dataclass
class BcftoolsParams:
    """bcftools mpileup + call knobs."""

    threads: int = 4
    # -d: mpileup's per-file depth cap. The default of 250 is bcftools' own;
    # raising it matters for high-coverage amplicon data, where truncating the
    # pileup loses real signal rather than noise.
    max_depth: int = 250

    def as_dict(self) -> dict:
        return {"threads": self.threads, "max_depth": self.max_depth}

    @classmethod
    def from_dict(cls, raw: dict | None) -> "BcftoolsParams":
        raw = raw or {}
        return cls(
            threads=int(raw.get("threads", 4)),
            max_depth=int(raw.get("max_depth", 250)),
        )


@dataclass
class VariantParams:
    """User-facing knobs for a variant calling run."""

    caller: VariantCaller = VariantCaller.CLAIR3
    threads: int = 4

    def as_dict(self) -> dict:
        return {"caller": self.caller.value, "threads": self.threads}

    @classmethod
    def from_dict(cls, raw: dict | None) -> "VariantParams":
        raw = dict(raw or {})

        caller_str = raw.get("caller", VariantCaller.CLAIR3.value)
        try:
            caller = VariantCaller(caller_str)
        except ValueError:
            raise ValidationError(
                f"Unknown variant caller {caller_str!r}",
                details={"valid": [c.value for c in VariantCaller]},
            ) from None

        threads = int(raw.get("threads", 4))
        if threads < 1:
            raise ValidationError("threads must be at least 1")

        return cls(caller=caller, threads=threads)


def caller_for_chemistry(chemistry: ReadChemistry) -> VariantCaller:
    """The variant caller suited to a read chemistry.

    CLR is refused outright rather than given a caller. Clair3 and DeepVariant
    are both trained on high-accuracy reads, and at CLR's error rate they
    produce calls that look ordinary and are wrong -- a worse outcome than
    refusing, because nothing downstream flags them.
    """
    if chemistry is ReadChemistry.CLR:
        raise ValidationError(
            "PacBio CLR reads are not suitable for variant calling: their "
            "error rate is too high for Clair3 or bcftools to produce "
            "reliable calls. Use HiFi/CCS reads instead.",
            details={"chemistry": chemistry.value},
        )
    if chemistry in (
        ReadChemistry.ONT_SIMPLEX,
        ReadChemistry.ONT_DUPLEX,
        ReadChemistry.HIFI,
    ):
        return VariantCaller.CLAIR3
    # SHORT and UNKNOWN both land on bcftools. Unknown means QC has not run;
    # short-read is both the common case and the safe guess, since Clair3 on
    # short reads would need an Illumina model this image does not ship.
    return VariantCaller.BCFTOOLS


def clair3_platform_for_chemistry(chemistry: ReadChemistry | None) -> str:
    """Clair3's --platform for a chemistry.

    Only two values matter here: HiFi has its own model, and both ONT modes
    share the one ONT model Clair3 ships. Duplex reads are more accurate than
    simplex but not differently modelled, so the distinction that matters for
    the aligner preset does not reach this far.
    """
    if chemistry is ReadChemistry.HIFI:
        return "hifi"
    return "ont"


def model_type_for_chemistry(chemistry: ReadChemistry | None) -> str:
    """DeepVariant's --model_type for a chemistry.

    Mirrors `clair3_platform_for_chemistry`. The image carries six models; only
    three are reachable from a chemistry we infer. WES is a real model but
    cannot be guessed from reads -- exome capture is a property of the library
    prep, not of the signal -- so it is left to an explicit user choice rather
    than inferred wrongly.
    """
    if chemistry is ReadChemistry.CLR:
        raise ValidationError(
            "PacBio CLR reads are not suitable for variant calling: their "
            "error rate is too high for DeepVariant's models to produce "
            "reliable calls. Use HiFi/CCS reads instead.",
            details={"chemistry": chemistry.value},
        )
    if chemistry in (ReadChemistry.ONT_SIMPLEX, ReadChemistry.ONT_DUPLEX):
        return "ONT_R104"
    if chemistry is ReadChemistry.HIFI:
        return "PACBIO"
    return "WGS"


def output_name(bam_name: str, caller: str) -> str:
    """The VCF name for an alignment.

    Derived from the BAM rather than the reads: two alignments of the same
    reads against different references are different evidence, and naming both
    outputs after the reads would collide.
    """
    stem = Path(bam_name).stem
    return f"{stem}.{caller}.vcf.gz"


def host_path_for(
    path: str | Path,
    *,
    container_root: str | None = None,
    host_root: str | None = None,
) -> str:
    """Where `path` lives on the Docker host.

    A sibling container started through the host's daemon mounts host paths, so
    the worker's own view of storage is the wrong thing to hand it. Passing
    `/data` to `docker run` mounts an empty directory that happens to exist,
    and the tool then fails "file not found" on a file that is plainly there --
    which is why anything outside the storage root raises here instead of being
    passed through hopefully.
    """
    container_root = (
        container_root if container_root is not None else str(settings.bioinfo_home)
    )
    host_root = host_root if host_root is not None else settings.bioinfo_home_host

    if not host_root:
        raise PermanentError(
            "BIOINFO_HOME_HOST is not set, so the host path for "
            f"{path} cannot be determined. Set it in docker-compose.override.yml "
            "to the same host directory BIOINFO_HOME is mounted from.",
            details={"path": str(path)},
        )

    # relative_to rather than a string prefix: `/database/x` starts with the
    # characters of `/data` without being under it, and translating it would
    # mount nothing.
    try:
        rel = Path(path).relative_to(Path(container_root))
    except ValueError:
        raise PermanentError(
            f"{path} is outside {container_root}, so it is not visible to a "
            "sibling container. Only files under the storage root can be "
            "passed to DeepVariant.",
            details={"path": str(path), "container_root": container_root},
        ) from None

    return str(Path(host_root) / rel) if str(rel) != "." else host_root


def build_clair3_command(
    *,
    clair3_path: str,
    bam: Path,
    reference: Path,
    output_dir: Path,
    model_path: Path,
    params: Clair3Params,
) -> list[str]:
    """Assemble the Clair3 invocation.

    `--include_all_ctgs` is passed unconditionally: without it Clair3 restricts
    calling to chr{1..22,X,Y}, which is human-specific and silently produces an
    empty VCF for a bacterial or plant assembly whose contigs are named
    anything else. An empty result that looks like a successful run is the
    worst failure mode available here, so this is not left to a default.
    """
    return [
        clair3_path,
        f"--bam_fn={bam}",
        f"--ref_fn={reference}",
        f"--threads={params.threads}",
        f"--platform={params.platform}",
        f"--model_path={model_path}",
        f"--output={output_dir}",
        "--include_all_ctgs",
    ]


def build_bcftools_command(
    *,
    bcftools_path: str,
    reference: Path,
    bam: Path,
    output: Path,
    params: BcftoolsParams,
) -> list[str]:
    """Assemble the bcftools mpileup -> call -> view pipeline.

    mpileup and call both stream BCF, which is cheap to pass between them;
    `view -O z` produces the bgzipped VCF that a .tbi index requires.

    Returned as `sh -o pipefail -c` for the same reason `build_align_command`
    is: a shell pipe reports the *last* command's exit status, so without
    pipefail a failed mpileup would surface as a successful run that produced
    an empty VCF. `sh` rather than `bash` -- the base image has no bash, and
    Debian trixie's dash supports `-o pipefail`.
    """
    mpileup = [
        bcftools_path,
        "mpileup",
        "-f",
        str(reference),
        "-d",
        str(params.max_depth),
        "-O",
        "u",  # uncompressed BCF: this is a pipe, so compressing is pure cost
        str(bam),
    ]
    call = [
        bcftools_path,
        "call",
        "-m",  # multiallelic caller, the current recommended default
        "-v",  # variants only; emitting every reference site is not useful here
        "-O",
        "u",
        "-",
    ]
    view = [
        bcftools_path,
        "view",
        "-O",
        "z",  # bgzipped VCF, required for .tbi
        "-o",
        str(output),
        "-",
    ]
    pipeline = f"{_quote(mpileup)} | {_quote(call)} | {_quote(view)}"
    return ["/bin/sh", "-o", "pipefail", "-c", pipeline]


def build_index_command(*, bcftools_path: str, vcf: Path) -> list[str]:
    """Index a bgzipped VCF, producing `<vcf>.tbi`.

    `bcftools index -t` rather than `tabix -p vcf`: one fewer binary to probe
    and install, and no need to guess whether the file is BGZF or plain gzip --
    bcftools wrote it and knows.
    """
    return [bcftools_path, "index", "-t", str(vcf)]


def _quote(argv: list[str]) -> str:
    """Shell-quote an argv for embedding in a `sh -c` string."""
    return " ".join(shlex.quote(a) for a in argv)


# Phase banners, most specific first: Clair3's full-alignment message also
# contains "pileup", so a looser match would leave the bar stuck on the wrong
# phase for the longest part of the run.
_PHASE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"full.?alignment", "full_alignment"),
    (r"merging", "merging"),
    (r"pileup", "pileup"),
    (r"calling variants", "calling"),
)

_PHASE_MESSAGES = {
    "pileup": "pileup calling",
    "full_alignment": "full-alignment calling",
    "merging": "merging outputs",
}


@dataclass
class VariantProgress:
    """Turns a caller's own output into a phase label.

    Deliberately not a percentage. Clair3 prints stage banners but no
    per-region progress, and bcftools' stderr has no per-contig line worth
    counting, so any fraction would be invented. The phase is genuinely useful
    -- full-alignment is the slow one, and knowing the run reached it is worth
    more than a number that does not mean anything.
    """

    phase: str = "calling"

    def feed(self, line: str) -> bool:
        """Consume a line. True if the caller should publish an update.

        False on a repeat of the current phase: these callbacks write to the
        database, and a banner printed on every line must not mean a write on
        every line.
        """
        for pattern, phase in _PHASE_PATTERNS:
            if re.search(pattern, line, re.IGNORECASE):
                if self.phase != phase:
                    self.phase = phase
                    return True
                return False
        return False

    @property
    def pct(self) -> float | None:
        """Always None: neither caller reports measurable progress."""
        return None

    def message(self) -> str:
        return _PHASE_MESSAGES.get(self.phase, "calling variants")
