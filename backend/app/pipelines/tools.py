"""External tool discovery and version capture.

A trimming parameter set means nothing without the version of the tool that
applied it -- that pair is what ends up in a methods section. Versions are read
once at first use and cached: they cannot change while the process is running,
and shelling out per job would add a process spawn to every run.

Resolution failures are surfaced through the API rather than raised, so a
missing binary shows up as "fastp not found" in the launch dialog instead of a
job that dies thirty seconds after the user walks away.
"""

import re
import shutil
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache

from app.config import settings
from app.errors import PermanentError
from app.logging import get_logger

log = get_logger(__name__)

# Long enough for a cold start on a loaded machine, short enough that a hung
# binary fails the probe rather than the request.
VERSION_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class Tool:
    name: str
    path: str | None  # absolute path, or None when not found
    version: str | None
    error: str | None = None

    @property
    def available(self) -> bool:
        return self.path is not None and self.error is None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "path": self.path,
            "version": self.version,
            "available": self.available,
            "error": self.error,
        }


def _probe(name: str, configured: str, version_args: list[str]) -> Tool:
    resolved = shutil.which(configured)
    if resolved is None:
        return Tool(
            name=name,
            path=None,
            version=None,
            error=(
                f"{configured!r} was not found on PATH. It is installed in the "
                f"backend image; if you are running outside Docker, install it "
                f"or set {name.upper()}_PATH."
            ),
        )

    try:
        proc = subprocess.run(
            [resolved, *version_args],
            capture_output=True,
            # Bytes, not text. A binary that cannot start prints whatever the
            # loader emits, and that is not guaranteed to be UTF-8 -- Rosetta's
            # "failed to open elf" message is not. Decoding with `text=True`
            # raised UnicodeDecodeError straight out of the probe, which turned
            # one broken tool into a failure of the whole tool panel.
            timeout=VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return Tool(name=name, path=resolved, version=None, error=str(e))

    # fastp writes its version to stderr, FastQC to stdout. Take whichever
    # produced something rather than guessing per tool.
    raw = _decode(proc.stdout) or _decode(proc.stderr)
    version = _clean_version(raw) or None

    # A tool that exits non-zero *and* says nothing recognizable is not usable,
    # whatever `which` found. The case that matters: an x86-64 binary on arm64
    # resolves on PATH and fails only when executed, and reporting it available
    # would defer that discovery to a job the user had walked away from.
    if proc.returncode != 0 and not _looks_like_version(version):
        detail = raw.strip().splitlines()[0] if raw.strip() else f"exited {proc.returncode}"
        return Tool(
            name=name,
            path=resolved,
            version=None,
            error=f"{configured!r} could not be run: {detail}",
        )

    return Tool(name=name, path=resolved, version=version)


def _decode(raw: bytes | None) -> str:
    """Best-effort text from a tool's output.

    `errors="replace"` rather than a raise: this output exists to be shown to a
    person, and an undecodable byte in a diagnostic message is not a reason to
    lose the message.
    """
    if not raw:
        return ""
    return raw.decode("utf-8", errors="replace").strip()


def _looks_like_version(value: str | None) -> bool:
    """Whether `_clean_version` found a real version rather than falling back.

    `_clean_version` returns its input verbatim when nothing parses, so a
    non-empty result is not by itself evidence that a version was found.
    """
    return bool(value and re.fullmatch(r"\d+\.\d+(?:\.\d+)?", value))


def _clean_version(raw: str) -> str:
    """A bare version number, from whichever line carries one.

    `fastp 0.24.0` and `FastQC v0.12.1` both become a bare version, so the UI
    can label them consistently.

    Scans lines rather than reading only the first, because bwa-mem2 has no
    `--version` flag: it prints a dispatch line naming the CPU-specific binary
    it selected (`Looking to launch executable "bwa-mem2.avx2"`) *before* the
    `Version: 2.2.1` line. Taking the first line captured that message instead,
    and a tool version that is quietly wrong is worse than one that is missing
    -- it is the half of a run's provenance that a methods section reports.
    """
    for line in (raw or "").splitlines():
        # Anchored to a digit-dot-digit so the `2` in "bwa-mem2.avx2" cannot
        # be mistaken for a version on the dispatch line.
        match = re.search(r"v?(\d+\.\d+(?:\.\d+)?)", line)
        if match:
            return match.group(1)
    return raw.splitlines()[0].strip() if raw else ""


@lru_cache(maxsize=1)
def fastp() -> Tool:
    return _probe("fastp", settings.fastp_path, ["--version"])


@lru_cache(maxsize=1)
def fastqc() -> Tool:
    return _probe("fastqc", settings.fastqc_path, ["--version"])


@lru_cache(maxsize=1)
def cutadapt() -> Tool:
    return _probe("cutadapt", settings.cutadapt_path, ["--version"])


@lru_cache(maxsize=1)
def trimmomatic() -> Tool:
    # Probed through TrimmomaticSE, not a bare `trimmomatic`: the Debian
    # package installs one wrapper per read layout (TrimmomaticPE and
    # TrimmomaticSE) around the same JAR and no combined entry point, so the
    # obvious name does not exist. Either wrapper reports the same version.
    return _probe("trimmomatic", settings.trimmomatic_path, ["-version"])


@lru_cache(maxsize=1)
def bwa_mem2() -> Tool:
    # bwa-mem2 has no --version flag: it prints usage, including the version,
    # and exits non-zero. `_probe` ignores the exit code and reads whichever
    # stream produced output, so the usage text is what gets parsed.
    return _probe("bwa-mem2", settings.bwa_mem2_path, ["version"])


@lru_cache(maxsize=1)
def minimap2() -> Tool:
    return _probe("minimap2", settings.minimap2_path, ["--version"])


@lru_cache(maxsize=1)
def bowtie2() -> Tool:
    return _probe("bowtie2", settings.bowtie2_path, ["--version"])


@lru_cache(maxsize=1)
def hisat2() -> Tool:
    return _probe("hisat2", settings.hisat2_path, ["--version"])


@lru_cache(maxsize=1)
def bowtie2_build() -> Tool:
    # The separate binary that builds a bowtie2 index (`aligners.layout_for`'s
    # `builder`) -- not a card in the tool panel, since a user never picks it
    # directly, but resolved the same way as every other tool path rather than
    # a bare `shutil.which` on a hardcoded name.
    return _probe("bowtie2-build", settings.bowtie2_build_path, ["--version"])


@lru_cache(maxsize=1)
def hisat2_build() -> Tool:
    return _probe("hisat2-build", settings.hisat2_build_path, ["--version"])


@lru_cache(maxsize=1)
def samtools() -> Tool:
    return _probe("samtools", settings.samtools_path, ["--version"])


@lru_cache(maxsize=1)
def bcftools() -> Tool:
    return _probe("bcftools", settings.bcftools_path, ["--version"])


@lru_cache(maxsize=1)
def clair3() -> Tool:
    # `--version` prints "Clair3 v2.0.2". `--help` also exits 0 but dumps a
    # usage block, and _clean_version scrapes a line of that into the version
    # field -- which then reaches the tool panel and every run's recorded
    # provenance as a garbled argument list.
    return _probe("clair3", settings.clair3_path, ["--version"])


@lru_cache(maxsize=1)
def nanoplot() -> Tool:
    return _probe("nanoplot", settings.nanoplot_path, ["--version"])


@lru_cache(maxsize=1)
def fasterq_dump() -> Tool:
    return _probe("fasterq-dump", settings.fasterq_dump_path, ["--version"])


@lru_cache(maxsize=1)
def prefetch() -> Tool:
    return _probe("prefetch", settings.prefetch_path, ["--version"])


def all_tools() -> list[Tool]:
    return [
        fastp(),
        fastqc(),
        cutadapt(),
        trimmomatic(),
        nanoplot(),
        bwa_mem2(),
        minimap2(),
        bowtie2(),
        hisat2(),
        samtools(),
        bcftools(),
        clair3(),
        fasterq_dump(),
        prefetch(),
    ]


# --- Static descriptions ----------------------------------------------------
#
# Defined once, here. The tool-selector UI consumes this table rather than
# carrying its own copy: two lists of tool descriptions drift, and the one in
# the frontend is the one nobody updates when a tool is added.


class PipelineType(StrEnum):
    TRIM = "trim"
    ALIGN = "align"
    QC = "qc"
    UTILITY = "utility"
    # Acquisition rather than analysis: these fetch data instead of
    # transforming it, so they belong on no analysis screen and would be
    # misleading listed as utilities beside samtools.
    DOWNLOAD = "download"
    VARIANT = "variant"


@dataclass(frozen=True)
class ToolMeta:
    # Plural, and a tuple, because membership is genuinely many-to-many: fastp
    # both trims and reports QC, and samtools is a utility whose flagstat
    # output is the alignment QC. A singular field would make each of those
    # disappear from one of the two lists it belongs in.
    pipelines: tuple[PipelineType, ...]
    summary: str
    strengths: tuple[str, ...]
    # The rail in the tool selector shows this; `summary` is a paragraph and
    # too long for a row. Kept beside it rather than derived by truncation,
    # since a sentence cut at 60 characters reads as a bug.
    one_liner: str = ""
    # Whether a job handler actually branches on this tool, independent of
    # whether the binary is installed. cutadapt and Trimmomatic probe cleanly
    # -- they are real, working binaries -- but trim_reads only knows fastp;
    # there is no cutadapt/Trimmomatic code path for it to dispatch into yet.
    # `available` (on `Tool`, not here) answers "is the binary usable"; this
    # answers "does anything in this application call it". A tool selector
    # conflating the two would offer a card that fails not with "not
    # installed" but with a confusing error from a handler that silently
    # ignored the choice.
    runnable: bool = True


TOOL_META: dict[str, ToolMeta] = {
    "fastp": ToolMeta(
        pipelines=(PipelineType.TRIM, PipelineType.QC),
        one_liner="All-in-one Illumina QC and adapter trimming",
        summary=(
            "All-in-one Illumina read QC and adapter trimming. Single-pass: "
            "quality filtering, adapter removal, poly-G tail trimming, length "
            "filtering, and duplicate detection. Produces structured JSON and "
            "HTML reports suitable for downstream charts and methods sections."
        ),
        strengths=(
            "Single-pass: trims and reports QC in one invocation",
            "Auto-detects adapter sequences from read overlap",
            "Handles NovaSeq/NextSeq two-colour poly-G artefacts",
            "Built-in per-base quality JSON for downstream visualization",
            "Fast C++ implementation, low memory footprint",
        ),
    ),
    "cutadapt": ToolMeta(
        pipelines=(PipelineType.TRIM,),
        one_liner="Flexible adapter, primer, and barcode trimming",
        summary=(
            "Flexible adapter, primer, and barcode trimmer for all sequencing "
            "platforms. Supports anchored adapters, linked adapters, "
            "demultiplexing by barcode, and adapter patterns fastp cannot "
            "express."
        ),
        strengths=(
            "Demultiplexing: split reads by barcode/index",
            "Linked adapter trimming for paired-end reads",
            "Anchored 5'/3' adapter matching for amplicon-seq",
            "Poly-A tail trimming for RNA-seq",
            "Works on any platform (Illumina, PacBio, ONT)",
        ),
    ),
    "trimmomatic": ToolMeta(
        pipelines=(PipelineType.TRIM,),
        one_liner="Classic sliding-window quality trimmer",
        summary=(
            "Classic sliding-window quality trimmer for Illumina paired-end "
            "and single-end reads. The longest-established tool in the field "
            "and still widely cited."
        ),
        strengths=(
            "Sliding-window quality trimming: aggressive on trailing bases",
            "Gold standard for legacy Illumina pipeline comparisons",
            "Simple paired-end model: keeps R1/R2 in sync",
            "Plays well with Nextera/TruSeq adapter FASTA files",
        ),
    ),
    "fastqc": ToolMeta(
        pipelines=(PipelineType.QC,),
        one_liner="The canonical per-file HTML QC report",
        summary=(
            "The canonical per-file HTML QC report. Per-base quality, GC "
            "content, overrepresented sequences, adapter content, sequence "
            "duplication levels -- the standard artifact for publication "
            "supplementary materials."
        ),
        strengths=(
            "The publication-standard QC report format",
            "Rich per-base visualizations (quality, GC, N)",
            "Overrepresented sequence detection",
            "Zero configuration: runs on any FASTQ",
        ),
    ),
    "nanoplot": ToolMeta(
        pipelines=(PipelineType.QC,),
        one_liner="QC plots for Nanopore and PacBio long reads",
        summary=(
            "QC for long reads. Plots read-length and quality distributions "
            "for Nanopore and PacBio data, where the per-base model FastQC "
            "assumes does not apply -- reads in one file can range from a few "
            "hundred bases to over 100 kb."
        ),
        strengths=(
            "Read-length distribution, the primary long-read quality signal",
            "Quality-vs-length plots that reveal truncated or degraded runs",
            "Handles Nanopore and PacBio HiFi alike",
            "Reads FASTQ, BAM, or a sequencing summary file",
        ),
    ),
    "fasterq-dump": ToolMeta(
        pipelines=(PipelineType.DOWNLOAD,),
        one_liner="Converts an SRA run into FASTQ",
        summary=(
            "Converts an SRA run into FASTQ. The multi-threaded successor to "
            "fastq-dump, and how sequencing data is pulled out of NCBI once a "
            "run accession is known."
        ),
        strengths=(
            "Multi-threaded: far faster than fastq-dump on large runs",
            "Splits paired-end runs into R1/R2 automatically",
            "Handles Illumina, PacBio, and Nanopore submissions alike",
        ),
    ),
    "prefetch": ToolMeta(
        pipelines=(PipelineType.DOWNLOAD,),
        one_liner="Fetches an SRA run into the local cache",
        summary=(
            "Fetches an SRA run into the local cache ahead of conversion. "
            "Some NCBI configurations require it before fasterq-dump, and it "
            "is a no-op when the run is already cached."
        ),
        strengths=(
            "Resumable: an interrupted fetch continues rather than restarting",
            "Validates the downloaded archive against its checksum",
            "A no-op on an already-cached run, so it is safe to always run",
        ),
    ),
    "bwa-mem2": ToolMeta(
        pipelines=(PipelineType.ALIGN,),
        one_liner="Standard short-read aligner for DNA-seq",
        summary=(
            "The standard short-read aligner for human and model organism "
            "genomes. Optimized for Illumina paired-end reads up to ~500 bp."
        ),
        strengths=(
            "Gold standard for Illumina WGS/WES/resequencing",
            "Handles mated reads with proper insert-size modeling",
            "2x faster than original bwa-mem with the same accuracy",
            "x86-64 (prebuilt) and arm64 (sse2neon build) supported",
        ),
    ),
    "minimap2": ToolMeta(
        pipelines=(PipelineType.ALIGN,),
        one_liner="Long-read and splice-aware aligner",
        summary=(
            "Versatile aligner for long reads (PacBio, Nanopore) and "
            "any-vs-any comparisons. Splice-aware for RNA-seq. Works on short "
            "reads with the -x sr preset."
        ),
        strengths=(
            "Designed for PacBio CLR/HiFi and ONT reads",
            "Splice-aware for RNA-seq (junctions in BAM tags)",
            "Short-read alignment with the -x sr preset",
            "Runs on all architectures including arm64",
        ),
    ),
    "bowtie2": ToolMeta(
        pipelines=(PipelineType.ALIGN,),
        one_liner="Short-read aligner for ChIP-seq, ATAC-seq, and resequencing",
        summary=(
            "Fast, memory-efficient short-read aligner. The standard choice "
            "for ChIP-seq and ATAC-seq, where its local alignment mode and "
            "explicit insert-size control matter more than the indel "
            "sensitivity a variant-calling pipeline wants."
        ),
        strengths=(
            "The conventional aligner for ChIP-seq and ATAC-seq",
            "Compact index: about 3.5 GB for a human genome",
            "Local mode soft-clips read ends rather than discarding the read",
            "Explicit insert-size ceiling for fragment-length-sensitive work",
            "Four sensitivity presets trading speed against divergent regions",
        ),
    ),
    "hisat2": ToolMeta(
        pipelines=(PipelineType.ALIGN,),
        one_liner="Splice-aware RNA-seq aligner with a compact graph index",
        summary=(
            "Splice-aware aligner built for RNA-seq. Its graph FM index is "
            "far smaller than STAR's for the same genome, which makes it the "
            "practical choice for transcriptome alignment on a machine that "
            "cannot spare 32 GB."
        ),
        strengths=(
            "Splice-aware: designed for RNA-seq junction discovery",
            "Compact index -- roughly 4 GB for human, against STAR's ~30 GB",
            "Strandness handling for dUTP and other stranded protocols",
            "Can be told to skip spliced alignment for DNA input",
            "Output mode tailored for downstream transcript assembly",
        ),
    ),
    "samtools": ToolMeta(
        pipelines=(PipelineType.UTILITY, PipelineType.QC),
        one_liner="Universal BAM/CRAM/SAM toolkit",
        summary=(
            "Universal BAM/CRAM/SAM toolkit. Sorting, indexing, flagstat, "
            "depth calculation. The common denominator of every alignment "
            "workflow."
        ),
        strengths=(
            "Universal BAM/CRAM manipulation",
            "Fast coordinate sorting and indexing",
            "Flagstat: comprehensive alignment statistics",
        ),
    ),
    "bcftools": ToolMeta(
        pipelines=(PipelineType.VARIANT, PipelineType.UTILITY),
        one_liner="Pileup variant caller and VCF toolkit",
        summary=(
            "Pileup-based variant caller and VCF/BCF toolkit. The long-"
            "established standard for short-read germline calling, and the "
            "tool that writes and indexes every VCF this pipeline produces."
        ),
        strengths=(
            "Lightweight and fast for single-sample calling",
            "Works on any aligner's BAM",
            "Part of the htslib/samtools ecosystem",
            "Also does the VCF indexing (bcftools index -t)",
        ),
    ),
    "clair3": ToolMeta(
        pipelines=(PipelineType.VARIANT,),
        one_liner="Deep-learning variant caller for long reads",
        summary=(
            "Deep-learning small-variant caller for long reads (ONT and "
            "PacBio HiFi). Combines a fast pileup model with a slower "
            "full-alignment model for high-accuracy SNV and indel calls."
        ),
        strengths=(
            "State-of-the-art accuracy on ONT and PacBio HiFi",
            "Calls SNVs and small indels together",
            "Chemistry-matched models, selected automatically from QC",
            "CPU-only build: no GPU required",
        ),
    ),
}


def tool_with_meta(tool: Tool) -> dict:
    """Probe result plus its static description, for the API.

    Enriched here at the boundary rather than by widening `Tool` itself: the
    probe result is what the pipeline code needs, and threading a summary
    string through `require()` would serve nothing but the one endpoint.
    """
    meta = TOOL_META.get(tool.name)
    return {
        **tool.as_dict(),
        "pipelines": [p.value for p in meta.pipelines] if meta else [],
        "summary": meta.summary if meta else "",
        "one_liner": meta.one_liner if meta else "",
        "strengths": list(meta.strengths) if meta else [],
        # Absent metadata defaults runnable to False too: a tool this
        # application does not describe is not one it has a code path for
        # either, and offering it as selectable would be worse than omitting
        # the summary text.
        "runnable": meta.runnable if meta else False,
    }


def all_tools_with_meta() -> list[dict]:
    return [tool_with_meta(t) for t in all_tools()]


def require(tool: Tool) -> Tool:
    """Assert a tool is usable before spending a job on it.

    PermanentError rather than RetryableError: a missing binary is not going to
    appear on its own, and burning the attempt budget on retries would only
    delay the error the user needs to see.
    """
    if not tool.available:
        raise PermanentError(
            f"{tool.name} is not available: {tool.error or 'unknown reason'}",
            details={"tool": tool.name, "path": tool.path},
        )
    return tool


def reset_cache() -> None:
    """Forget probed versions. For tests, and for a config change at runtime."""
    fastp.cache_clear()
    fastqc.cache_clear()
    cutadapt.cache_clear()
    trimmomatic.cache_clear()
    nanoplot.cache_clear()
    bwa_mem2.cache_clear()
    minimap2.cache_clear()
    bowtie2.cache_clear()
    hisat2.cache_clear()
    bowtie2_build.cache_clear()
    hisat2_build.cache_clear()
    samtools.cache_clear()
    bcftools.cache_clear()
    clair3.cache_clear()
    fasterq_dump.cache_clear()
    prefetch.cache_clear()
