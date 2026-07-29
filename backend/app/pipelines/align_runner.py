"""Building and observing an alignment run.

Kept separate from the job handler so the parts worth testing -- command
construction, progress parsing, flagstat extraction -- are pure functions over
strings and dicts, with no queue or filesystem involved.
"""

import re
import shlex
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from app.errors import ValidationError
from app.logging import get_logger
from app.pipelines import aligners
from app.pipelines.aligners import Aligner

# Parameter classes moved to align_params.py when the second and third
# aligners arrived: one flat class covering four tools would be a union of
# mostly-inapplicable fields. Re-exported here because this is where every
# existing call site imports them from.
from app.pipelines.align_params import (
    BaseAlignParams,
    Bowtie2Params,
    Bwa2Params,
    Hisat2Params,
    Minimap2Params,
)

log = get_logger(__name__)

# AlignParams aliases Minimap2Params rather than BaseAlignParams: every
# existing direct-construction call site (AlignParams(), AlignParams(preset=...))
# relies on defaults and fields that only Minimap2Params has, and dataclass
# `__init__` performs no validation -- that only lives in `from_dict` -- so
# constructing a Minimap2Params with aligner=Aligner.BWA_MEM2 works exactly
# like the old flat dataclass did.
AlignParams = Minimap2Params

# bwa-mem2 reports throughput on stderr as it goes:
#   [M::mem_process_seqs] Processed 80000 reads in 12.345 CPU sec
# minimap2 says nothing comparable per-batch, so a minimap2 run reports phase
# only. Better an honest indeterminate bar than an invented one.
_PROCESSED_RE = re.compile(r"Processed\s+(\d+)\s+reads", re.IGNORECASE)

# samtools sort announces its merge phase, which is the tail of the run.
_MERGING_RE = re.compile(r"merging from \d+ files", re.IGNORECASE)

# As in trimming: the read total is an estimate extrapolated at ingest, so a
# bar driven by it must never claim completion.
MAX_MEASURED_PCT = 0.95


class Preset:
    """minimap2 presets. Not cosmetic: the wrong preset for long reads produces
    silently poor alignments rather than an error."""

    MAP_ONT = "map-ont"
    MAP_PB = "map-pb"
    MAP_HIFI = "map-hifi"  # PacBio HiFi/CCS: ~Q30, since minimap2 2.19
    LR_HQ = "lr:hq"  # ONT duplex / Q20+ chemistry
    SHORT_READ = "sr"

    ALL = (MAP_ONT, MAP_PB, MAP_HIFI, LR_HQ, SHORT_READ)


class ReadChemistry(StrEnum):
    """How accurate a long-read file actually is, independent of who made it.

    `sam_platform` answers "who made this" (ONT/PACBIO/ILLUMINA); it cannot
    answer "how accurate is it", which is the question the minimap2 preset
    actually needs. HiFi and CLR are both PACBIO_SMRT in SRA and both PACBIO
    in SAM, so this has to be a separate axis rather than folded into platform.
    """

    HIFI = "hifi"
    CLR = "clr"
    ONT_SIMPLEX = "ont_simplex"
    ONT_DUPLEX = "ont_duplex"
    SHORT = "short"
    UNKNOWN = "unknown"


_CHEMISTRY_PRESETS: dict[ReadChemistry, str] = {
    ReadChemistry.HIFI: Preset.MAP_HIFI,
    ReadChemistry.CLR: Preset.MAP_PB,
    ReadChemistry.ONT_SIMPLEX: Preset.MAP_ONT,
    ReadChemistry.ONT_DUPLEX: Preset.LR_HQ,
    ReadChemistry.SHORT: Preset.SHORT_READ,
}


def preset_for_chemistry(chemistry: ReadChemistry) -> str:
    """The minimap2 preset for a read chemistry, defaulting to short-read.

    UNKNOWN falls back to short-read here; callers that have a platform to
    fall back on (see `suggested_preset`) should prefer that over this
    default, since "unknown chemistry on a PacBio file" should stay map-pb,
    not become sr.
    """
    return _CHEMISTRY_PRESETS.get(chemistry, Preset.SHORT_READ)


@dataclass(frozen=True)
class ReadGroup:
    """`@RG` header fields.

    Required rather than optional: GATK and most variant callers refuse to run
    without a read group, and adding one afterwards means rewriting the whole
    BAM. Defaulted from the reads' existing metadata at launch, so this is
    usually a confirmation rather than data entry.
    """

    sample: str
    library: str
    platform: str
    identifier: str | None = None

    def as_sam_header(self) -> str:
        """The `@RG` line, tab-separated as the SAM spec requires.

        Emitted with literal backslash-t rather than real tabs: this string is
        passed as a single argv element to `-R`, and the aligners parse the
        two-character escape themselves.
        """
        rg_id = self.identifier or self.sample
        fields = [
            "@RG",
            f"ID:{rg_id}",
            f"SM:{self.sample}",
            f"LB:{self.library}",
            f"PL:{self.platform}",
        ]
        return "\\t".join(fields)

    def as_rg_args(self) -> list[str]:
        """`--rg-id` plus one `--rg` per remaining field.

        bowtie2 and HISAT2 have no single -R taking a whole @RG line. Handing
        them `as_sam_header()` would embed a literal backslash-t in the BAM
        header, which reads as a corrupt read group to every downstream tool
        rather than failing at alignment time.
        """
        rg_id = self.identifier or self.sample
        args = ["--rg-id", rg_id]
        for field_value in (
            f"SM:{self.sample}",
            f"LB:{self.library}",
            f"PL:{self.platform}",
        ):
            args += ["--rg", field_value]
        return args

    def as_dict(self) -> dict:
        return {
            "sample": self.sample,
            "library": self.library,
            "platform": self.platform,
            "identifier": self.identifier,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "ReadGroup":
        data = data or {}
        missing = [k for k in ("sample", "library", "platform") if not data.get(k)]
        if missing:
            raise ValidationError(
                f"Read group requires {', '.join(missing)}",
                details={"missing": missing},
            )
        return cls(
            sample=str(data["sample"]),
            library=str(data["library"]),
            platform=str(data["platform"]),
            identifier=str(data["identifier"]) if data.get("identifier") else None,
        )


def default_preset(aligner: Aligner) -> str:
    """bwa-mem2 has a single short-read mode; minimap2 must be told."""
    return "" if aligner is Aligner.BWA_MEM2 else Preset.SHORT_READ


def build_index_command(
    *, aligner: Aligner, tool_path: str, reference: Path, output: Path | None = None
) -> list[str]:
    """The command that builds an aligner's index for a reference.

    Three shapes: bwa-mem2 writes its five files beside the reference and
    takes no output path, minimap2 writes one file wherever it is told, and
    bowtie2/HISAT2 take a reference and a basename as two positional
    arguments. `tool_path` for the latter two is the *builder* binary
    (bowtie2-build, hisat2-build), not the aligner -- see
    `aligners.layout_for(...).builder`.
    """
    if aligner is Aligner.BWA_MEM2:
        return [tool_path, "index", str(reference)]

    if aligner is Aligner.MINIMAP2:
        if output is None:
            raise ValidationError("minimap2 index requires an output path")
        return [tool_path, "-d", str(output), str(reference)]

    # bowtie2-build / hisat2-build: <reference> <basename>. The basename is
    # the reference path itself, so the index files land beside it as
    # `genome.fna.1.bt2` and materialize back under names the layout knows.
    return [tool_path, str(reference), str(reference)]


def build_faidx_command(*, samtools_path: str, reference: Path) -> list[str]:
    return [samtools_path, "faidx", str(reference)]


def build_align_command(
    *,
    aligner: Aligner,
    aligner_path: str,
    samtools_path: str,
    reference: Path,
    r1: Path,
    r2: Path | None,
    output: Path,
    read_group: ReadGroup,
    params: AlignParams,
    tmp_prefix: Path | None = None,
) -> list[str]:
    """Align and coordinate-sort in one pipeline, as a `/bin/sh` invocation.

    Piping straight into `samtools sort` never materializes the intermediate
    SAM, which is several times the size of the resulting BAM and pure waste to
    write.

    Returned as an explicit `sh -o pipefail -c` argv rather than a bare command
    list, because the exit status of a shell pipe is the *last* command's:
    `bwa-mem2 | samtools sort` reports samtools' success even when bwa died
    halfway, and the result is a truncated BAM that looks fine. `pipefail` is
    what makes a failing first stage fail the job.

    `sh` rather than `bash`: the base image has no bash, and `-o pipefail` is
    supported by Debian's dash as of the version trixie ships.
    """
    align_argv = _aligner_argv(
        aligner=aligner,
        aligner_path=aligner_path,
        reference=reference,
        r1=r1,
        r2=r2,
        read_group=read_group,
        params=params,
    )

    sort_argv = [
        samtools_path,
        "sort",
        "-@",
        str(max(params.threads - 1, 1)),
        "-m",
        f"{params.sort_memory_mb}M",
        "-o",
        str(output),
    ]
    if tmp_prefix is not None:
        # Keeps samtools' spill files inside the job's scratch directory, so a
        # crashed run leaves nothing in the system temp dir for nobody to reap.
        sort_argv += ["-T", str(tmp_prefix)]

    pipeline = f"{_quote(align_argv)} | {_quote(sort_argv)}"
    return ["/bin/sh", "-o", "pipefail", "-c", pipeline]


def _aligner_argv(
    *,
    aligner: Aligner,
    aligner_path: str,
    reference: Path,
    r1: Path,
    r2: Path | None,
    read_group: ReadGroup,
    params,
) -> list[str]:
    """The aligner half of the pipeline, before samtools.

    Four tools, three calling conventions. bwa-mem2 and minimap2 take reads
    positionally and the reference as a path; bowtie2 and HISAT2 take the
    index basename via -x and the reads via -U or -1/-2. Getting that wrong
    does not fail cleanly -- bowtie2 reads a stray positional argument as its
    index basename and reports a missing index.
    """
    if aligner is Aligner.BWA_MEM2:
        argv = [aligner_path, "mem", "-t", str(params.threads)]
        argv += ["-R", read_group.as_sam_header()]
        argv += [str(reference), str(r1)]
        if r2 is not None:
            argv.append(str(r2))
        return argv

    if aligner is Aligner.MINIMAP2:
        # -a emits SAM rather than PAF, which samtools sort requires.
        argv = [aligner_path, "-a", "-x", params.preset, "-t", str(params.threads)]
        argv += ["-R", read_group.as_sam_header(), str(reference), str(r1)]
        if r2 is not None:
            argv.append(str(r2))
        return argv

    return _prefix_aligner_argv(
        aligner=aligner,
        aligner_path=aligner_path,
        reference=reference,
        r1=r1,
        r2=r2,
        read_group=read_group,
        params=params,
    )


def _prefix_aligner_argv(
    *,
    aligner: Aligner,
    aligner_path: str,
    reference: Path,
    r1: Path,
    r2: Path | None,
    read_group: ReadGroup,
    params,
) -> list[str]:
    """bowtie2 and HISAT2, which share a calling convention."""
    layout = aligners.layout_for(aligner)
    argv = [aligner_path, "-x", layout.reference_argument(reference)]

    if r2 is not None:
        argv += ["-1", str(r1), "-2", str(r2)]
    else:
        argv += ["-U", str(r1)]

    argv += ["-p", str(params.threads)]
    argv += read_group.as_rg_args()

    if params.report_k > 0:
        # 0 means "leave the flag off". `-k 0` tells the tool to report zero
        # alignments, which produces an empty BAM rather than an error.
        argv += ["-k", str(params.report_k)]

    if aligner is Aligner.BOWTIE2:
        argv.append(params.sensitivity)
        if params.local:
            argv.append("--local")
        argv += ["-X", str(params.maxins)]
        if params.no_mixed:
            argv.append("--no-mixed")
        if params.no_discordant:
            argv.append("--no-discordant")
    else:
        if params.rna_strandness:
            # The flag has no "unstranded" value -- omitting it is how that is
            # expressed, and an empty string would be rejected as an argument.
            argv += ["--rna-strandness", params.rna_strandness]
        argv += ["--max-intronlen", str(params.max_intronlen)]
        if params.no_spliced_alignment:
            argv.append("--no-spliced-alignment")
        if params.dta:
            argv.append("--dta")

    return argv


def build_index_bam_command(*, samtools_path: str, bam: Path) -> list[str]:
    return [samtools_path, "index", str(bam)]


def build_flagstat_command(*, samtools_path: str, bam: Path) -> list[str]:
    return [samtools_path, "flagstat", str(bam)]


def build_markdup_command(
    *,
    samtools_path: str,
    source: Path,
    output: Path,
    threads: int,
    paired: bool,
    tmp_prefix: Path | None = None,
) -> list[str]:
    """Mark duplicates on a coordinate-sorted BAM.

    Standard for DNA-seq variant calling and wrong for RNA-seq and amplicon
    data, where duplicate reads are the expected consequence of the protocol
    rather than a PCR artifact -- so this is a real choice, not a default.

    `markdup` picks which read of a pair to flag using the `ms` (mate score)
    tag, which only `fixmate -m` writes -- and fixmate needs mates adjacent,
    i.e. name-sorted input, while markdup needs the usual coordinate order.
    So for paired data this is name-sort -> fixmate -> coordinate-sort ->
    markdup, piped together to avoid materializing three intermediate BAMs.
    Single-end reads have no mate to score, and `markdup` runs directly on
    the (already coordinate-sorted) input.
    """
    worker_threads = str(max(threads - 1, 1))
    markdup_argv = [samtools_path, "markdup", "-@", str(threads), str(source), str(output)]
    if not paired:
        return markdup_argv

    name_sort_argv = [samtools_path, "sort", "-@", worker_threads, "-n", "-o", "-", str(source)]
    fixmate_argv = [samtools_path, "fixmate", "-@", worker_threads, "-m", "-", "-"]
    coord_sort_argv = [samtools_path, "sort", "-@", worker_threads, "-o", "-"]
    if tmp_prefix is not None:
        coord_sort_argv += ["-T", str(tmp_prefix)]
    coord_sort_argv.append("-")
    markdup_argv[-2] = "-"  # read the coordinate-sorted stream, not `source`

    pipeline = " | ".join(
        _quote(argv) for argv in (name_sort_argv, fixmate_argv, coord_sort_argv, markdup_argv)
    )
    return ["/bin/sh", "-o", "pipefail", "-c", pipeline]


def _quote(argv: list[str]) -> str:
    """Shell-quote an argv for embedding in a `sh -c` string.

    Every element goes through shlex.quote: filenames come from user-facing
    object names, and one with a space in it would otherwise split into two
    arguments -- turning a valid run into a confusing "file not found".
    """
    return " ".join(shlex.quote(a) for a in argv)


def parse_flagstat(text: str) -> dict:
    """Extract the summary numbers from `samtools flagstat` output.

    Read as part of index_bam because the file is already being traversed, and
    these four numbers are what a person actually checks before trusting an
    alignment.
    """
    facts: dict = {}

    total = re.search(r"^(\d+)\s+\+\s+(\d+)\s+in total", text, re.MULTILINE)
    if total:
        facts["total_reads"] = int(total.group(1)) + int(total.group(2))

    mapped = re.search(r"^(\d+)\s+\+\s+(\d+)\s+mapped\s*\(", text, re.MULTILINE)
    if mapped:
        facts["mapped_reads"] = int(mapped.group(1)) + int(mapped.group(2))

    paired = re.search(
        r"^(\d+)\s+\+\s+(\d+)\s+properly paired\s*\(", text, re.MULTILINE
    )
    if paired:
        facts["properly_paired_reads"] = int(paired.group(1)) + int(paired.group(2))

    dups = re.search(r"^(\d+)\s+\+\s+(\d+)\s+duplicates", text, re.MULTILINE)
    if dups:
        facts["duplicate_reads"] = int(dups.group(1)) + int(dups.group(2))

    # Rates are derived here rather than parsed from flagstat's own percentages,
    # which it prints as "N/A" when the denominator is zero.
    total_reads = facts.get("total_reads")
    if total_reads:
        if "mapped_reads" in facts:
            facts["mapped_pct"] = round(100 * facts["mapped_reads"] / total_reads, 2)
        if "properly_paired_reads" in facts:
            facts["properly_paired_pct"] = round(
                100 * facts["properly_paired_reads"] / total_reads, 2
            )
        if "duplicate_reads" in facts:
            facts["duplicate_pct"] = round(100 * facts["duplicate_reads"] / total_reads, 2)

    return facts


@dataclass
class AlignProgress:
    """Turns an aligner's own output into a progress fraction.

    Same honesty constraint as trimming: the read total is an estimate
    extrapolated at ingest, so the bar caps below complete rather than claiming
    a completion it cannot verify.
    """

    expected_reads: int | None = None
    processed: int = 0
    phase: str = "aligning"

    def feed(self, line: str) -> bool:
        """Consume a line. True if the caller should publish an update."""
        if _MERGING_RE.search(line):
            self.phase = "sorting"
            return True

        match = _PROCESSED_RE.search(line)
        if not match:
            return False

        # Cumulative, not a running total: bwa-mem2 reports per batch.
        self.processed += int(match.group(1))
        return True

    @property
    def pct(self) -> float | None:
        if not self.expected_reads:
            return None
        return min(self.processed / self.expected_reads, MAX_MEASURED_PCT)

    def message(self) -> str:
        if self.phase == "sorting":
            return "sorting alignments"
        if self.processed:
            return f"aligned {self.processed:,} reads"
        return "aligning"
