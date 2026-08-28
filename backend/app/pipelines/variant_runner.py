"""Building and observing a variant calling run.

Kept separate from the job handler so the parts worth testing -- command
construction, caller selection, progress parsing -- are pure functions over
strings and paths, with no queue or filesystem involved. Mirrors
`align_runner.py`, which splits the same way for the same reason.
"""

import re
import shlex
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from app.config import is_arm64, settings
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
class DeepVariantParams:
    """DeepVariant invocation knobs."""

    threads: int = 4
    model_type: str = "WGS"  # {WGS, WES, PACBIO, ONT_R104, HYBRID_PACBIO_ILLUMINA, MASSEQ}

    def as_dict(self) -> dict:
        return {"threads": self.threads, "model_type": self.model_type}

    @classmethod
    def from_dict(cls, raw: dict | None) -> "DeepVariantParams":
        raw = raw or {}
        return cls(
            threads=int(raw.get("threads", 4)),
            model_type=str(raw.get("model_type", "WGS")),
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
        # Names the file for *this* machine's role. A child node runs from
        # docker-compose.child-node.yml and has no override file, so the old
        # advice sent the one person who could hit this to a file that does not
        # exist there (#880).
        raise PermanentError(
            "BIOINFO_HOME_HOST is not set, so the host path for "
            f"{path} cannot be determined. Set it to the same host directory "
            "BIOINFO_HOME is mounted from -- in docker-compose.override.yml on "
            "the primary, or in this node's .env if this is a compute node.",
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


def build_deepvariant_command(
    *,
    image: str,
    bam: Path,
    reference: Path,
    output_vcf: Path,
    params: DeepVariantParams,
    container_root: str | None = None,
    host_root: str | None = None,
    arm64: bool | None = None,
) -> list[str]:
    """Assemble the `docker run` invocation for DeepVariant.

    Two path spaces are in play and mixing them is the failure this function
    exists to prevent. The *mount* uses the host path, because the daemon
    starting the container reads it from the host filesystem. The tool's own
    arguments stay container paths, because inside the sibling container the
    mount lands at the same place the worker sees it. So exactly one value is
    translated -- the left half of `-v` -- and everything else is passed
    through unchanged.

    `arm64` selects the fastmath workaround below; it defaults to this
    machine's architecture and is a parameter so the tests can assert both
    branches without patching the interpreter.
    """
    host_root_path = host_path_for(
        container_root if container_root is not None else str(settings.bioinfo_home),
        container_root=container_root,
        host_root=host_root,
    )
    # Validated, not used: raises for anything outside the storage root, which
    # would mount nothing and fail confusingly deep inside the tool.
    for p in (bam, reference, output_vcf):
        host_path_for(p, container_root=container_root, host_root=host_root)

    mount_at = container_root if container_root is not None else str(settings.bioinfo_home)

    # arm64 only, and deliberately not passed on x86-64. Without these the
    # arm64 port dies with SIGILL inside TensorFlow: it targets Graviton3 and
    # defaults to BF16 fastmath, while Docker on macOS advertises `bf16` in
    # /proc/cpuinfo but faults on the instruction. Measured 2026-08-01 -- see
    # the validation section of the design doc.
    #
    # On x86-64 there is no such fault, and passing them anyway is not
    # harmless: TF_ENABLE_ONEDNN_OPTS=0 turns off the oneDNN kernels that make
    # DeepVariant tolerable on a CPU, so it would be a silent, large slowdown
    # rather than a visible error.
    fastmath_env = (
        ["-e", "DNNL_DEFAULT_FPMATH_MODE=STRICT", "-e", "TF_ENABLE_ONEDNN_OPTS=0"]
        if (is_arm64() if arm64 is None else arm64)
        else []
    )

    return [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{host_root_path}:{mount_at}",
        *fastmath_env,
        image,
        "run_deepvariant",
        f"--model_type={params.model_type}",
        f"--ref={reference}",
        f"--reads={bam}",
        f"--output_vcf={output_vcf}",
        f"--num_shards={params.threads}",
    ]


def build_index_command(*, bcftools_path: str, vcf: Path) -> list[str]:
    """Index a bgzipped VCF, producing `<vcf>.tbi`.

    `bcftools index -t` rather than `tabix -p vcf`: one fewer binary to probe
    and install, and no need to guess whether the file is BGZF or plain gzip --
    bcftools wrote it and knows.

    `-f` because not every caller leaves this step something to do. Clair3 and
    bcftools produce a bare VCF, but DeepVariant writes its own `.tbi` beside
    the output -- and without `-f`, bcftools treats an existing index as an
    error and exits 1, failing a job whose calling stage had already finished.
    Overwriting beats skipping the step per caller: re-indexing takes seconds
    against a run of minutes, and the alternative teaches this function which
    tools happen to emit an index today, which is exactly the kind of fact that
    goes stale silently.
    """
    return [bcftools_path, "index", "-t", "-f", str(vcf)]


def _quote(argv: list[str]) -> str:
    """Shell-quote an argv for embedding in a `sh -c` string."""
    return " ".join(shlex.quote(a) for a in argv)


# Phase banners, most specific first: Clair3's full-alignment message also
# contains "pileup", so a looser match would leave the bar stuck on the wrong
# phase for the longest part of the run.
#
# Anchored on Clair3's actual numbered-stage banners (e.g. "1/7 Call variants
# using pileup model") rather than loose substrings. A real captured run
# (tests/fixtures/tool_logs/clair3-v2.0.2.log) showed why looser patterns
# fail: "merging" never matches Clair3's real "Merge pileup VCF..." banner
# (capitalized, different word); a bare "pileup"/"full.?alignment" substring
# also matches per-contig summary lines near the end of a run ("Pileup
# variants processed in <contig>: N") and flips the phase back and forth
# repeatedly right as the job is finishing; and "full.?alignment" alone
# matches an early config-echo line ("ENABLE NO PHASING FOR FULL ALIGNMENT")
# before any full-alignment work has actually started.
_PHASE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"call.*low-quality.*full-alignment model", "full_alignment"),
    (r"merge pileup vcf", "merging"),
    (r"call variants using pileup model", "pileup"),
)

_PHASE_MESSAGES = {
    "pileup": "pileup calling",
    "full_alignment": "full-alignment calling",
    "merging": "merging outputs",
}

# Clair3 declares its whole chunk plan up front, one line naming the contigs
# and the next naming how many chunks each was split into, in the same order:
#   [INFO] Call variant in contigs: NC_001135.5 NC_001147.6 NC_001142.9 ...
#   [INFO] Chunk number for each contig: 1 1 1 1 1
# Summed, that total is fixed for the whole pileup phase -- a genuine
# units_total, not an estimate. bcftools and full-alignment calling have no
# equivalent line, so those stay phase-only exactly as before; this only
# feeds units on the pileup phase, where Clair3 does most of its per-chunk
# work. Verified against a real captured run
# (tests/fixtures/tool_logs/clair3-v2.0.2.log) before writing, per the
# convention that cost a bug once already (see the golden-fixture module
# docstring for align_runner's minimap2 history).
_CHUNK_PLAN_RE = re.compile(r"Chunk number for each contig:\s*([\d\s]+)$")

# Each chunk announces its own completion once, naming the contig it belongs
# to and its position in that contig's chunk count -- not a running total,
# a single event to count.
#   Total processed positions in NC_001147.6 (chunk 1/1) : 0
_CHUNK_DONE_RE = re.compile(r"Total processed positions in (\S+) \(chunk (\d+)/(\d+)\)")


@dataclass
class VariantProgress:
    """Turns a caller's own output into a phase label, and for Clair3's
    pileup phase, a chunk count.

    Deliberately no percentage: Clair3's chunks vary enormously in size (a
    contig with no reads is one *and* a contig with millions is also one), so
    "N of M chunks" is honest in a way a fraction derived from it would not
    be -- see units_total's docstring above. bcftools' stderr has no
    per-contig line worth counting at all, so it stays phase-only. The phase
    itself is genuinely useful regardless -- full-alignment is the slow one,
    and knowing the run reached it is worth more than a number that does not
    mean anything.
    """

    name: str = "variant-caller"
    phase: str = "calling"
    units_total: int | None = None
    _chunks_seen: set[tuple[str, str]] = field(default_factory=set)

    def feed(self, line: str) -> bool:
        """Consume a line. True if the caller should publish an update.

        False on a repeat phase banner: these callbacks write to the
        database, and a banner printed on every line must not mean a write on
        every line. A chunk-completion line always publishes, since each one
        is a distinct event rather than a restatement of known state.
        """
        changed = False

        for pattern, phase in _PHASE_PATTERNS:
            if re.search(pattern, line, re.IGNORECASE):
                if self.phase != phase:
                    self.phase = phase
                    changed = True
                break

        plan = _CHUNK_PLAN_RE.search(line)
        if plan:
            self.units_total = sum(int(n) for n in plan.group(1).split())
            changed = True

        done = _CHUNK_DONE_RE.search(line)
        if done:
            key = (done.group(1), done.group(2))
            if key not in self._chunks_seen:
                self._chunks_seen.add(key)
                changed = True

        return changed

    @property
    def units_done(self) -> int | None:
        return len(self._chunks_seen) if self._chunks_seen else None

    @property
    def pct(self) -> float | None:
        """Always None: neither caller reports a measurable fraction, only
        chunk counts -- see the class docstring for why those stay units
        rather than being turned into one."""
        return None

    def message(self) -> str:
        base = _PHASE_MESSAGES.get(self.phase, "calling variants")
        if self.units_done and self.units_total:
            return f"{base}: {self.units_done}/{self.units_total} chunks"
        return base

    def snapshot(self) -> dict:
        result = {"pct": self.pct, "phase": self.phase, "message": self.message()}
        if self.units_total is not None:
            result["units_total"] = self.units_total
            result["units_done"] = self.units_done or 0
            result["unit_label"] = "chunks"
        return result
