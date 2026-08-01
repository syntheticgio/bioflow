"""External tool discovery and version capture.

A trimming parameter set means nothing without the version of the tool that
applied it -- that pair is what ends up in a methods section. Versions are read
once at first use and cached: they cannot change while the process is running,
and shelling out per job would add a process spawn to every run.

Resolution failures are surfaced through the API rather than raised, so a
missing binary shows up as "fastp not found" in the launch dialog instead of a
job that dies thirty seconds after the user walks away.
"""

import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from enum import StrEnum
from functools import lru_cache

from app.config import settings
from app.errors import PermanentError
from app.logging import get_logger

log = get_logger(__name__)

# Long enough for a cold start on a loaded machine, short enough that a hung
# binary fails the probe rather than the request.
VERSION_TIMEOUT_SECONDS = 10

# For a tool whose `--version` has to import a scientific Python stack before
# it can answer. NanoPlot pulls in pandas, scipy and plotly on the way to
# printing one line: measured at 16.3s in a cold container against 2-4s once
# the imports are in page cache, so the 10s default failed it exactly when the
# app was starting up and probing for the first time. The failure was silent
# and load-dependent -- `available` went False, the long-read QC path vanished,
# and a warm re-probe then showed the tool working fine.
#
# Note this is not "Python entry points are slow": cutadapt is one too and
# answers in 0.2s. What costs the time is the import graph behind the entry
# point, so this belongs on the individual tool rather than on a class of them.
SLOW_IMPORT_TIMEOUT_SECONDS = 60


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


# Probe results supplied from outside, keyed by tool name and holding the
# fingerprint of the binary they describe. Populated at startup from Redis (see
# `tool_cache.py`) so a restart does not re-pay the probe cost -- NanoPlot alone
# is 12s of it.
#
# Keyed by fingerprint rather than trusted outright: an entry describing a
# binary that has since been upgraded must be ignored, not served.
_seeded: dict[str, tuple[str, Tool]] = {}


def seed(name: str, fingerprint: str | None, tool: Tool) -> None:
    """Offer a previously-probed result for `name`.

    Ignored at use time unless the binary still fingerprints identically, so a
    caller cannot force a stale version into the cache.
    """
    if fingerprint is None:
        return
    _seeded[name] = (fingerprint, tool)


def _probe(
    name: str,
    configured: str,
    version_args: list[str],
    # Resolved at call time, not bound as a default: the hang test patches
    # VERSION_TIMEOUT_SECONDS on the module, and a default evaluated at import
    # would capture 10 and quietly ignore the patch -- a test that takes the
    # full timeout while appearing to control it.
    timeout: float | None = None,
) -> Tool:
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

    seeded = _seeded.get(name)
    if seeded is not None and seeded[0] == _fingerprint(resolved):
        return seeded[1]

    try:
        proc = subprocess.run(
            [resolved, *version_args],
            capture_output=True,
            # Bytes, not text. A binary that cannot start prints whatever the
            # loader emits, and that is not guaranteed to be UTF-8 -- Rosetta's
            # "failed to open elf" message is not. Decoding with `text=True`
            # raised UnicodeDecodeError straight out of the probe, which turned
            # one broken tool into a failure of the whole tool panel.
            timeout=VERSION_TIMEOUT_SECONDS if timeout is None else timeout,
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


def _fingerprint(path: str | None) -> str | None:
    """Identity of the binary at `path`, for deciding whether a cached probe
    still describes it.

    Returns None when there is nothing to fingerprint -- an unresolved tool, or
    one that vanished between `which` and here. None means "always probe",
    which is the safe direction: a cached version string that no longer matches
    the installed binary is the half of a run's provenance a methods section
    reports.

    Combines path, mtime/size and a content hash, because each catches a change
    the others miss. Content alone drops path, so two tools resolving to
    identical bytes -- or one tool moving to a new PATH entry unchanged --
    would look the same. mtime/size alone missed an in-place same-size
    replacement landing in one filesystem timestamp tick, measured happening in
    this repo's test container. And mtime moves on a package upgrade even when
    the bytes do not.

    Known gap: fastqc, bowtie2, hisat2 and cutadapt are interpreter wrappers
    that dispatch to a separate payload (JARs, installed packages). This
    fingerprints the wrapper, not the payload -- so an upgrade replacing only
    the payload, leaving the wrapper byte- and mtime-identical, goes
    undetected. Accepted rather than silent; the probe cache's TTL is the
    backstop.
    """
    if path is None:
        return None
    try:
        st = os.stat(path)
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return f"{path}:{st.st_mtime_ns}:{st.st_size}:{digest.hexdigest()}"


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


# `bcftools csq` landed in 1.7. Older builds have the binary and not the
# subcommand, which fails at run time rather than at probe time.
CSQ_MIN_VERSION = (1, 7)


@lru_cache(maxsize=1)
def bcftools_csq() -> Tool:
    """The consequence caller, as a capability of an already-probed binary.

    Not a `_probe` call: `csq` is a subcommand, so there is no separate
    executable to find and no `--version` of its own. What can go wrong is a
    bcftools too old to have it, and the Actions card needs to say that rather
    than "bcftools is missing" -- which would be false and would send the user
    looking for an install that is already there.
    """
    base = bcftools()
    if not base.available:
        return Tool(
            name="bcftools csq",
            path=None,
            version=None,
            error=f"bcftools is unavailable, so csq cannot run: {base.error}",
        )

    # An unparseable version is not evidence of being too old. Treating it as
    # such would disable a working tool over a cosmetic parse failure, so the
    # check only fires when a real version was read and it is below the floor.
    if _looks_like_version(base.version):
        parts = tuple(int(p) for p in base.version.split(".")[:2])
        if parts < CSQ_MIN_VERSION:
            return Tool(
                name="bcftools csq",
                path=base.path,
                version=base.version,
                error=(
                    f"bcftools {base.version} has no `csq` subcommand; "
                    f"{CSQ_MIN_VERSION[0]}.{CSQ_MIN_VERSION[1]} or newer is required."
                ),
            )

    # `path` is bcftools' own, because csq is invoked *through* it -- so this
    # is still the binary a caller execs, and `name` is the capability rather
    # than an executable. That is also why this is absent from `all_tools()`:
    # that list drives the tool panel, which enumerates installed binaries and
    # pairs each with a TOOL_META entry. A capability has no separate install
    # to report and no meta row, so listing it would render a blank card for
    # something the user cannot act on. Reached through `require()` at job
    # time instead, and surfaced by the Actions card's own reason text.
    return Tool(name="bcftools csq", path=base.path, version=base.version)


@lru_cache(maxsize=1)
def clair3() -> Tool:
    # `--version` prints "Clair3 v2.0.2". `--help` also exits 0 but dumps a
    # usage block, and _clean_version scrapes a line of that into the version
    # field -- which then reaches the tool panel and every run's recorded
    # provenance as a garbled argument list.
    return _probe("clair3", settings.clair3_path, ["--version"])


@lru_cache(maxsize=1)
def deepvariant() -> Tool:
    """Whether DeepVariant can be run, which is a question about Docker.

    Unlike every other tool here there is no binary to find: DeepVariant runs
    from its own image as a sibling container, because vendoring 2.3GB of
    TensorFlow on a second Python runtime into this image to gain one caller is
    a bad trade. So the probe asks whether we can reach a Docker daemon, and
    reports the image reference as the version -- the tag is the provenance a
    methods section would cite, and there is no `--version` to ask.

    The `path` returned here is the *docker client's* path, not DeepVariant's
    -- there is no DeepVariant binary. That path is real and fingerprints
    successfully, which means the probe cache in `tool_cache.py` would
    otherwise persist this result keyed to the docker client's identity, and
    it would not change when the image is pulled or removed. `tool_cache.warm`
    excludes this tool by name for exactly that reason; see the comment there.
    """
    client = shutil.which("docker")
    if client is None:
        return Tool(
            name="deepvariant",
            path=None,
            version=None,
            error=(
                "No docker client in this container, so DeepVariant's image "
                "cannot be run. It runs as a sibling container rather than "
                "being installed here."
            ),
        )

    try:
        proc = subprocess.run(
            [client, "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            timeout=VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return Tool(name="deepvariant", path=client, version=None, error=str(e))

    if proc.returncode != 0:
        detail = _decode(proc.stderr) or _decode(proc.stdout) or "unknown error"
        return Tool(
            name="deepvariant",
            path=client,
            version=None,
            error=f"Docker daemon is not reachable: {detail.splitlines()[0]}",
        )

    return Tool(
        name="deepvariant",
        path=client,
        version=settings.deepvariant_image.rsplit("/", 1)[-1],
    )


@lru_cache(maxsize=1)
def nanoplot() -> Tool:
    # Slow timeout: see SLOW_IMPORT_TIMEOUT_SECONDS. NanoPlot spends its
    # startup importing pandas/scipy/plotly before it will print a version.
    return _probe(
        "nanoplot",
        settings.nanoplot_path,
        ["--version"],
        timeout=SLOW_IMPORT_TIMEOUT_SECONDS,
    )


@lru_cache(maxsize=1)
def fasterq_dump() -> Tool:
    return _probe("fasterq-dump", settings.fasterq_dump_path, ["--version"])


@lru_cache(maxsize=1)
def prefetch() -> Tool:
    return _probe("prefetch", settings.prefetch_path, ["--version"])


@lru_cache(maxsize=1)
def datasets() -> Tool:
    return _probe("datasets", settings.datasets_path, ["--version"])


@lru_cache(maxsize=1)
def featurecounts() -> Tool:
    # Writes its banner to stderr and exits non-zero on `-v` with no input
    # files. `_probe` already reads whichever stream produced something, and
    # already accepts a non-zero exit when the output still looks like a
    # version, so both are handled without a special case here.
    return _probe("featurecounts", settings.featurecounts_path, ["-v"])


@lru_cache(maxsize=1)
def pydeseq2() -> Tool:
    """The differential expression engine.

    A Python library, not a binary, so `_probe`'s shutil.which model does not
    apply -- there is nothing on PATH to find and nothing to exec. It is
    reported as a tool anyway, deliberately: the version that ran a test is
    half of that result's provenance in exactly the way an aligner version is,
    and a DE result whose engine version is not recorded anywhere is not
    reproducible. Leaving it out of the panel would mean the one number a
    methods section needs is the one number the app does not show.

    `path` carries the installed module's file rather than a PATH entry, which
    keeps `available` (path is not None and no error) meaning the same thing
    it means for every other tool, so `require()` needs no special case.
    """
    try:
        import pydeseq2 as _pydeseq2
    except ImportError as e:
        return Tool(
            name="pydeseq2",
            path=None,
            version=None,
            error=(
                "pydeseq2 is not importable. It is pip-installed in the "
                f"backend image; if you are running outside Docker, install "
                f"it with `pip install pydeseq2`. ({e})"
            ),
        )

    return Tool(
        name="pydeseq2",
        path=getattr(_pydeseq2, "__file__", None) or "pydeseq2",
        version=getattr(_pydeseq2, "__version__", None),
    )


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
        deepvariant(),
        fasterq_dump(),
        prefetch(),
        datasets(),
        featurecounts(),
        pydeseq2(),
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
    # Counting reads per gene and testing those counts between conditions.
    # One member rather than two: quantification and the test are separate
    # pipelines, but a tool selector splitting them would show two screens of
    # one card each, and a user thinking "RNA-seq" is looking for both.
    EXPRESSION = "expression"


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
    # whether the binary is installed. `available` (on `Tool`, not here)
    # answers "is the binary usable"; this answers "does anything in this
    # application call it". A tool selector conflating the two would offer a
    # card that fails not with "not installed" but with a confusing error
    # from a handler that silently ignored the choice.
    #
    # This comment used to cite cutadapt and Trimmomatic as the unrunnable
    # examples. That stopped being true once trim_reads grew its three-way
    # dispatch (pipeline_handlers.py) and was not updated -- it then misled a
    # later change into documenting both tools as unwired on a user-facing
    # page. No entry below sets this to False today; the default that matters
    # is the False in tool_with_meta's fallback, for a tool with no entry here
    # at all.
    runnable: bool = True

    # --- Reference data for the Software help page. ---
    # These are bibliographic facts about the tool rather than anything the
    # pipeline consults, but they live here because this dict is already the
    # one registry of what a tool *is*. A second catalog keyed by tool name
    # would go stale silently -- a new tool would simply be missing from the
    # help page with nothing failing, which is the trap
    # suggestion_service.py's hand-maintained mapping already set once.
    #
    # All default to "" so an entry stays constructible while it is being
    # filled in. `test_every_tool_is_documented` is what actually requires
    # them, and it deliberately exempts the two that are legitimately absent
    # for some tools (repository, citation_url).
    homepage: str = ""
    repository: str = ""
    citation: str = ""  # human-readable, for a methods section
    citation_url: str = ""
    license: str = ""  # SPDX identifier
    # How *this application* uses the tool -- the one thing here that no
    # upstream page can tell a user. Prose, so nothing can verify it
    # mechanically: describe behaviour, not flags, so it survives a
    # parameter change in the runner.
    usage: str = ""


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
        homepage="https://github.com/OpenGene/fastp",
        repository="https://github.com/OpenGene/fastp",
        # fastp's README asks for the 2025 iMeta paper, which supersedes the
        # 2018 Bioinformatics one most pipelines still cite.
        citation="Chen, iMeta 2025",
        citation_url="https://doi.org/10.1002/imt2.70078",
        license="MIT",
        usage=(
            "The default trimmer, and the only one the Actions tab suggests. "
            "Adapter-trims and quality-filters a FASTQ file or an R1/R2 pair, "
            "and its JSON report supplies the per-base quality and duplication "
            "numbers the QC screen charts."
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
        homepage="https://cutadapt.readthedocs.io/",
        repository="https://github.com/marcelm/cutadapt",
        citation="Martin, EMBnet.journal 2011",
        citation_url="https://doi.org/10.14806/ej.17.1.200",
        license="MIT",
        usage=(
            "One of the three trimmers a trim job can select. Trims the "
            "selected reads and parses its own JSON report for the read counts "
            "the trim summary shows; it reports no progress while running, "
            "since it emits no progress stream to follow."
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
        # The project's own site (usadellab.org/cms/?page=trimmomatic) is
        # HTTP-only, and a plain-http homepage renders as a browser warning
        # next to a tool we are vouching for. The repo is the same project
        # over TLS.
        homepage="https://github.com/usadellab/Trimmomatic",
        repository="https://github.com/usadellab/Trimmomatic",
        citation="Bolger, Lohse & Usadel, Bioinformatics 2014",
        citation_url="https://doi.org/10.1093/bioinformatics/btu170",
        # GPL-3.0 per the repo's LICENSE, with a stated carve-out: the bundled
        # Illumina adapter sequences remain Illumina's and are included by
        # permission rather than under the GPL.
        # GPL-3.0-only, not -or-later: the LICENSE grants version 3 and the
        # sources carry no per-file "or (at your option) any later version"
        # header, unlike FastQC, bowtie2, and hisat2.
        license="GPL-3.0-only",
        usage=(
            "One of the three trimmers a trim job can select. Runs sliding- "
            "window trimming against one of the adapter FASTA files this image "
            "ships, and its summary line supplies the surviving-read counts "
            "the trim report shows. The adapter file is checked against that "
            "shipped set before use, since it reaches an unescaped argument."
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
        homepage="https://www.bioinformatics.babraham.ac.uk/projects/fastqc/",
        repository="https://github.com/s-andrews/FastQC",
        # No paper: FastQC has always been cited as a web reference, and the
        # project has never published one. Hence the empty citation_url.
        citation="Andrews, FastQC (Babraham Bioinformatics)",
        license="GPL-3.0-or-later",
        usage=(
            "Runs alongside fastp on every short-read QC job, producing the "
            "standalone HTML report the QC screen links. Treated as optional: "
            "if the binary is missing the QC job still finishes on fastp's "
            "numbers rather than failing."
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
        homepage="https://github.com/wdecoster/NanoPlot",
        repository="https://github.com/wdecoster/NanoPlot",
        citation="De Coster & Rademakers, Bioinformatics 2023",
        citation_url="https://doi.org/10.1093/bioinformatics/btad311",
        license="MIT",
        usage=(
            "The QC path for long reads: a QC job on Nanopore or PacBio input "
            "runs NanoPlot instead of fastp and FastQC. Its plot directory "
            "becomes the QC report, and its summary statistics supply the read "
            "counts and length figures the QC screen shows."
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
        homepage="https://github.com/ncbi/sra-tools",
        repository="https://github.com/ncbi/sra-tools",
        # No paper for the toolkit itself; the archive it reads is what gets
        # cited, and that is the reference NCBI points submitters to.
        citation="Leinonen et al., Nucleic Acids Research 2011 (Sequence Read Archive)",
        citation_url="https://doi.org/10.1093/nar/gkq1019",
        # A US Government Work, dedicated to the public domain rather than
        # licensed -- SPDX has an identifier for exactly this notice.
        license="NCBI-PD",
        usage=(
            "The second half of an SRA download job: converts the fetched run "
            "into FASTQ, splitting a paired run into R1/R2, and the resulting "
            "files are registered as project objects. Unlike prefetch its "
            "failure fails the job, since nothing downstream exists without it."
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
        # Same repository, licence, and reference as fasterq-dump: both are
        # binaries of the one SRA Toolkit, listed separately because a user
        # picks them separately.
        homepage="https://github.com/ncbi/sra-tools",
        repository="https://github.com/ncbi/sra-tools",
        citation="Leinonen et al., Nucleic Acids Research 2011 (Sequence Read Archive)",
        citation_url="https://doi.org/10.1093/nar/gkq1019",
        license="NCBI-PD",
        usage=(
            "Runs first on every SRA download job, caching the run before "
            "conversion. Its failure is logged rather than fatal -- "
            "fasterq-dump can fetch on its own -- so a prefetch that cannot "
            "run only costs resumability, not the download."
        ),
    ),
    "datasets": ToolMeta(
        pipelines=(PipelineType.DOWNLOAD,),
        summary=(
            "NCBI's Datasets CLI. Downloads a published assembly -- genome "
            "FASTA, annotation, protein and CDS sequences -- from a GenBank "
            "(GCA) or RefSeq (GCF) accession, which is how reference genomes "
            "arrive without a manual trip to the NCBI website."
        ),
        strengths=(
            "One accession fetches genome, annotation, protein and CDS together",
            "Ships an md5 manifest, so a truncated transfer is detectable",
            "Reports package contents and size before downloading anything",
        ),
        one_liner="Downloads a published genome assembly from NCBI",
        homepage="https://www.ncbi.nlm.nih.gov/datasets/",
        repository="https://github.com/ncbi/datasets",
        citation="O'Leary et al., Scientific Data 2024",
        citation_url="https://doi.org/10.1038/s41597-024-03571-y",
        license="NCBI-PD",
        usage=(
            "Backs the 'download a reference assembly' job: given a GCA or GCF "
            "accession it fetches the assembly package, and the genome FASTA "
            "and annotation it unpacks are registered as project objects "
            "available to align against."
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
        homepage="https://github.com/bwa-mem2/bwa-mem2",
        repository="https://github.com/bwa-mem2/bwa-mem2",
        citation="Vasimuddin et al., IEEE IPDPS 2019",
        citation_url="https://doi.org/10.1109/IPDPS.2019.00041",
        license="MIT",
        usage=(
            "One of the four aligners an alignment job can select, and the "
            "default suggested for short reads. Builds its own index for a "
            "reference the first time one is needed, then aligns straight into "
            "a sorted BAM."
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
        homepage="https://github.com/lh3/minimap2",
        repository="https://github.com/lh3/minimap2",
        citation="Li, Bioinformatics 2018",
        citation_url="https://doi.org/10.1093/bioinformatics/bty191",
        license="MIT",
        usage=(
            "The aligner for long reads, and the one suggested for Nanopore or "
            "PacBio input. Aligns straight into a sorted BAM, with the preset "
            "matching the read type offered as a choice in the align dialog."
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
        homepage="https://bowtie-bio.sourceforge.net/bowtie2/index.shtml",
        repository="https://github.com/BenLangmead/bowtie2",
        citation="Langmead & Salzberg, Nature Methods 2012",
        citation_url="https://doi.org/10.1038/nmeth.1923",
        license="GPL-3.0-or-later",
        usage=(
            "One of the four aligners an alignment job can select. Its index is "
            "built by a separate bowtie2-build binary, which this application "
            "runs on demand rather than asking the user to prepare a reference "
            "beforehand."
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
        homepage="https://daehwankimlab.github.io/hisat2/",
        repository="https://github.com/DaehwanKimLab/hisat2",
        citation="Kim et al., Nature Biotechnology 2019",
        citation_url="https://doi.org/10.1038/s41587-019-0201-4",
        license="GPL-3.0-or-later",
        usage=(
            "One of the four aligners an alignment job can select, and the "
            "RNA-seq choice. Like bowtie2 its index comes from a separate "
            "hisat2-build binary this application runs on demand."
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
        homepage="https://www.htslib.org/",
        repository="https://github.com/samtools/samtools",
        # The project's README asks for the 2021 GigaScience paper rather than
        # the 2009 SAM-format one, which is the citation most pipelines still
        # reach for.
        citation="Danecek et al., GigaScience 2021",
        citation_url="https://doi.org/10.1093/gigascience/giab008",
        license="MIT",
        usage=(
            "The most-used tool here: every alignment job pipes into it to "
            "sort and index the BAM, it indexes the reference beforehand, and "
            "its flagstat, idxstats, and coverage output are the numbers "
            "behind the alignment report."
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
        homepage="https://www.htslib.org/",
        repository="https://github.com/samtools/bcftools",
        # The same GigaScience paper samtools cites: bcftools' own README asks
        # for it by name rather than for a separate bcftools paper. The
        # calling model itself is Li 2011, doi.org/10.1093/bioinformatics/btr509.
        citation="Danecek et al., GigaScience 2021",
        citation_url="https://doi.org/10.1093/gigascience/giab008",
        # Dual-licensed, unlike samtools' plain MIT: the LICENSE offers a
        # choice of MIT/Expat or GPL, and a build linked against the GNU
        # Scientific Library (off by default) is GPL-only.
        license="MIT OR GPL-3.0-or-later",
        usage=(
            "The short-read variant caller, and the indexer for every VCF this "
            "application produces -- including the ones Clair3 wrote, since it "
            "is installed whichever caller ran."
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
        homepage="https://github.com/HKU-BAL/Clair3",
        repository="https://github.com/HKU-BAL/Clair3",
        citation="Zheng et al., Nature Computational Science 2022",
        citation_url="https://doi.org/10.1038/s43588-022-00387-x",
        license="BSD-3-Clause",
        usage=(
            "The long-read variant caller: a variant job on ONT or PacBio HiFi "
            "input runs Clair3 rather than bcftools, against a chemistry-"
            "matched model picked from the reads' recorded platform. bcftools "
            "still indexes the VCF it writes."
        ),
    ),
    "deepvariant": ToolMeta(
        pipelines=(PipelineType.VARIANT,),
        one_liner="Deep-learning variant caller from Google",
        summary=(
            "A deep-learning variant caller from Google. Turns the pileup at "
            "each position into an image and classifies it with a "
            "convolutional network, rather than applying a statistical model."
        ),
        strengths=(
            "Consistently high accuracy on short-read SNVs and small indels",
            "Models trained per sequencing chemistry",
        ),
        homepage="https://github.com/google/deepvariant",
        # Upstream, because that is what BioFlow runs on x86-64 -- which is now
        # the common case, and was not when this pointed at the arm64 port. The
        # port is named in `usage` instead, where the architecture it applies to
        # can be stated alongside it. Both repositories were checked with
        # `gh api repos/.../license` on 2026-08-01 and both report
        # BSD-3-Clause, so the field below is correct on either architecture
        # rather than assuming the port inherits upstream's.
        repository="https://github.com/google/deepvariant",
        citation=(
            "Poplin R, et al. A universal SNP and small-indel variant caller "
            "using deep neural networks. Nat Biotechnol. 2018."
        ),
        citation_url="https://doi.org/10.1038/nbt.4235",
        license="BSD-3-Clause",
        usage=(
            "Runs as a separate container image rather than being installed "
            "in the BioFlow image, and is downloaded the first time it is "
            "used. BioFlow picks the model from the reads' inferred "
            "chemistry. Which image is used depends on the machine: upstream "
            "publishes x86-64 only, so on arm64 BioFlow uses a community port "
            "instead. Neither image is multi-architecture."
        ),
        runnable=True,
    ),
    "featurecounts": ToolMeta(
        pipelines=(PipelineType.EXPRESSION,),
        one_liner="Counts reads per gene from an aligned BAM",
        summary=(
            "Assigns aligned reads to genomic features and counts them per "
            "gene. The step between an RNA-seq alignment and any differential "
            "expression test: a BAM says where reads landed, and this turns "
            "that into one number per gene per sample."
        ),
        strengths=(
            "Fast: counts a typical RNA-seq BAM in well under a minute",
            "Handles paired-end fragments without double-counting mates",
            "Strand-specific counting for stranded library preps",
            "Reads GTF directly, including NCBI's published annotations",
        ),
        homepage="https://subread.sourceforge.net/",
        repository="https://github.com/ShiLab-Bioinformatics/subread",
        citation=(
            "Liao Y, Smyth GK, Shi W. featureCounts: an efficient "
            "general-purpose program for assigning sequence reads to genomic "
            "features. Bioinformatics. 2014;30(7):923-30."
        ),
        citation_url="https://doi.org/10.1093/bioinformatics/btt656",
        # From Debian's copyright file for subread 2.0.8+dfsg-1, which is the
        # build actually in this image. The project's own homepage does not
        # state a license anywhere, so upstream could not confirm it.
        license="GPL-3.0+",
        usage=(
            "Runs one sample at a time, taking an aligned RNA-seq BAM and an "
            "annotation and producing a per-gene count file registered as a "
            "project object. One sample per job rather than all of them in "
            "one invocation, so adding a sample later costs one job instead "
            "of redoing every sample; the differential expression job is what "
            "merges the per-sample counts back into a matrix. Strandedness "
            "follows the alignment's recorded library orientation where the "
            "BAM has one, since a mismatch there silently returns near-zero "
            "counts rather than failing."
        ),
    ),
    "pydeseq2": ToolMeta(
        pipelines=(PipelineType.EXPRESSION,),
        one_liner="Differential expression testing on count data",
        summary=(
            "Tests whether per-gene counts differ between conditions, fitting "
            "a negative binomial model with shrunken dispersion estimates. A "
            "Python reimplementation of DESeq2's method, and the step that "
            "turns a counts matrix into a ranked list of genes with fold "
            "changes and multiple-testing-corrected p-values."
        ),
        strengths=(
            "Models count data properly rather than treating it as continuous",
            "Shares dispersion information across genes, which is what makes "
            "small replicate numbers usable",
            "Corrects for multiple testing across every gene tested",
            "Same method as DESeq2 without requiring an R runtime",
        ),
        homepage="https://pydeseq2.readthedocs.io/en/stable/",
        repository="https://github.com/owkin/PyDESeq2",
        citation=(
            "Muzellec B, Telenczuk M, Cabeli V, Andreux M. PyDESeq2: a python "
            "package for bulk RNA-seq differential expression analysis. "
            "Bioinformatics. 2023."
        ),
        citation_url="https://doi.org/10.1093/bioinformatics/btad547",
        license="MIT",
        usage=(
            "Backs the differential expression job. Takes the per-gene count "
            "files produced by quantification plus the condition each sample "
            "belongs to, fits the model, and returns per-gene fold changes "
            "and adjusted p-values. A Python library rather than a binary, so "
            "it runs inside the worker process instead of being executed; its "
            "version is still reported here because it is half the provenance "
            "of any result it produces."
        ),
    ),
}


def tool_with_meta(tool: Tool) -> dict:
    """Probe result plus its static description, for the API.

    Enriched here at the boundary rather than by widening `Tool` itself: the
    probe result is what the pipeline code needs, and threading a summary
    string through `require()` would serve nothing but the one endpoint.

    Built via `asdict` on `ToolMeta` rather than naming each field, the same
    pattern `aligner_registry.schema_for` already uses: a field added to
    `ToolMeta` reaches the API without a second edit here. `pipelines` is the
    one exception, since it needs its enum members converted to their string
    values for JSON. `strengths` is wrapped in `list(...)` explicitly because
    `asdict` preserves tuples as tuples rather than converting them to lists.
    """
    meta = TOOL_META.get(tool.name)
    meta_dict = (
        asdict(meta)
        if meta
        else {
            "pipelines": (),
            "summary": "",
            "strengths": (),
            "one_liner": "",
            # Absent metadata defaults runnable to False too: a tool this
            # application does not describe is not one it has a code path for
            # either, and offering it as selectable would be worse than
            # omitting the summary text.
            "runnable": False,
            "homepage": "",
            "repository": "",
            "citation": "",
            "citation_url": "",
            "license": "",
            "usage": "",
        }
    )
    return {
        **tool.as_dict(),
        **meta_dict,
        "pipelines": [p.value for p in meta_dict["pipelines"]],
        "strengths": list(meta_dict["strengths"]),
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
    _seeded.clear()
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
    bcftools_csq.cache_clear()
    clair3.cache_clear()
    deepvariant.cache_clear()
    fasterq_dump.cache_clear()
    prefetch.cache_clear()
    datasets.cache_clear()
    featurecounts.cache_clear()
    pydeseq2.cache_clear()
