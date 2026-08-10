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
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from functools import lru_cache

from app.config import is_arm64, settings
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


class InstallState(StrEnum):
    """Whether an ON_DEMAND_IMAGE tool's image has actually been pulled.

    Distinct from `Tool.available` on purpose: `available` answers "can this
    run right now", which for a not-installed optional tool is correctly
    False, but the UI needs to tell "not installed" (an offer -- press
    Install) apart from "broken" (a fault -- something is wrong). Folding
    both into one boolean is what let the old `deepvariant()` probe report
    `available=True` for an image that had never been pulled: it only asked
    whether the Docker daemon answered, never whether the image existed, so
    `suggestion_service` offered the card, `require()` passed, and the job
    was accepted only to die later at `_require_image` telling the user to
    open a terminal. See docs/superpowers/specs/
    2026-08-05-optional-tool-delivery-design.md.

    NOT_INSTALLED is a real, expected state on a first run -- not an error.
    UNKNOWN is the only one that is: no docker client, or a daemon that will
    not answer, both of which are genuine faults rather than "you haven't
    installed this yet."
    """

    INSTALLED = "installed"
    NOT_INSTALLED = "not_installed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Tool:
    name: str
    path: str | None  # absolute path, or None when not found
    version: str | None
    error: str | None = None
    # None for every BUNDLED tool -- the concept does not apply to a binary
    # found on PATH, only to an ON_DEMAND_IMAGE tool probed by image
    # presence. Set only by probes that actually check, so a tool nobody has
    # wired up for on-demand delivery cannot silently claim NOT_INSTALLED.
    install_state: InstallState | None = None

    @property
    def available(self) -> bool:
        if self.install_state is not None:
            return (
                self.path is not None
                and self.error is None
                and self.install_state is InstallState.INSTALLED
            )
        return self.path is not None and self.error is None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "path": self.path,
            "version": self.version,
            "available": self.available,
            "error": self.error,
            "install_state": self.install_state.value if self.install_state else None,
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


def cached_version(name: str) -> str | None:
    """Whatever version was last seeded for `name`, without probing.

    Deliberately does not re-validate the fingerprint the way `_probe`'s
    callers do: a stale entry is acceptable for a *record of what ran*, in a
    way it would not be for a capability check. Must never grow a probe
    fallback -- this is called from the executor's `finally`, and a miss
    triggering a real probe (NanoPlot alone is 12s cold) would delay every
    job's completion on the way to recording it.
    """
    seeded = _seeded.get(name)
    return seeded[1].version if seeded else None


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
    return bool(value and re.fullmatch(_VERSION_PATTERN, value))


# The trailing letter is STAR's: it releases 2.7.11a and 2.7.11b as distinct
# versions, and truncating to 2.7.11 names a release that is not the one that
# ran. Kept to a single letter so it cannot swallow a suffix that is not part
# of the version -- minimap2's "2.28-r1209" still parses as 2.28, since the
# separator is a dash rather than a letter.
_VERSION_PATTERN = r"\d+\.\d+(?:\.\d+)?[a-z]?"


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
        match = re.search(rf"v?({_VERSION_PATTERN})", line)
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
def star() -> Tool:
    # `--version` prints the bare version and exits 0. Note the Debian package
    # installs /usr/bin/STAR as a dispatcher that execs STAR-avx2 or a plainer
    # build depending on the CPU, so the fingerprint covers the dispatcher and
    # not the binary that ultimately runs -- the same known gap the interpreter
    # wrappers have, and accepted for the same reason.
    return _probe("star", settings.star_path, ["--version"])


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
def bgzip() -> Tool:
    """The compressor the storage layer uses at ingest, not a pipeline step.

    Ships as part of the `tabix` package alongside htslib, already installed
    for its indexing (`.tbi`) role. Probed the same way as every other tool so
    a missing binary shows up as `available=False` rather than an ingest job
    that fails thirty seconds in -- the storage layer checks this and falls
    back to Python's stdlib gzip rather than failing the ingest outright.
    """
    return _probe("bgzip", settings.bgzip_path, ["--version"])


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


def _probe_on_demand_image(name: str, image: str) -> Tool:
    """Whether an ON_DEMAND_IMAGE tool can be run, which is a question about
    Docker rather than about a binary on PATH.

    Generalized from what was, until this function existed, a DeepVariant-only
    probe -- so that a second tool moving to this delivery model (Clair3,
    per docs/superpowers/plans/2026-08-05-optional-tool-delivery.md's task 8)
    is a table entry, not a second copy of this logic.

    Two checks, in order, because they answer different questions and must not
    be conflated: first, can the daemon be reached at all (a real fault, not
    an install state -- UNKNOWN); second, given a reachable daemon, has this
    specific image actually been pulled (INSTALLED or NOT_INSTALLED, both
    expected states, neither an error). The probe this replaced only asked the
    first question and reported `available=True` whenever it succeeded, never
    checking the image was present -- so a card was offered, `require()`
    passed, and the job was accepted only to die later telling the user to run
    `docker pull` from a terminal. See docs/superpowers/specs/
    2026-08-05-optional-tool-delivery-design.md.

    The `path` returned here is the *docker client's* path, not the tool's --
    there is no binary for an image-delivered tool. That path is real and
    fingerprints successfully, which means the probe cache in `tool_cache.py`
    would otherwise persist a result keyed to the docker client's identity,
    unchanged by the image being pulled or removed. `NOT_FINGERPRINTABLE`
    excludes every ON_DEMAND_IMAGE tool by name for exactly that reason.
    """
    client = shutil.which("docker")
    if client is None:
        return Tool(
            name=name,
            path=None,
            version=None,
            error=(
                f"No docker client in this container, so {name}'s image "
                "cannot be run. It runs as a sibling container rather than "
                "being installed here."
            ),
            install_state=InstallState.UNKNOWN,
        )

    try:
        daemon_proc = subprocess.run(
            [client, "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            timeout=VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return Tool(
            name=name,
            path=client,
            version=None,
            error=str(e),
            install_state=InstallState.UNKNOWN,
        )

    if daemon_proc.returncode != 0:
        detail = (
            _decode(daemon_proc.stderr) or _decode(daemon_proc.stdout) or "unknown error"
        )
        return Tool(
            name=name,
            path=client,
            version=None,
            error=f"Docker daemon is not reachable: {detail.splitlines()[0]}",
            install_state=InstallState.UNKNOWN,
        )

    try:
        inspect_proc = subprocess.run(
            [client, "image", "inspect", image],
            capture_output=True,
            timeout=VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return Tool(
            name=name,
            path=client,
            version=None,
            error=str(e),
            install_state=InstallState.UNKNOWN,
        )

    if inspect_proc.returncode != 0:
        # Not installed is the expected state on a first run, not a fault --
        # the wording here is what the UNAVAILABLE card path renders today,
        # and an offer to install reads very differently from "not found".
        return Tool(
            name=name,
            path=client,
            version=None,
            error=(
                f"{name} is not installed. It runs as a separate container "
                f"image ({image}) rather than being bundled here, and is "
                "downloaded on first use."
            ),
            install_state=InstallState.NOT_INSTALLED,
        )

    return Tool(
        name=name,
        path=client,
        version=image.rsplit("/", 1)[-1],
        install_state=InstallState.INSTALLED,
    )


@lru_cache(maxsize=1)
def deepvariant() -> Tool:
    """Whether DeepVariant is installed and runnable.

    DeepVariant runs from its own image as a sibling container rather than a
    binary in this one, because vendoring 2.3GB of TensorFlow on a second
    Python runtime into this image to gain one caller is a bad trade. See
    `_probe_on_demand_image` for what "installed" actually checks and why.
    """
    return _probe_on_demand_image("deepvariant", settings.deepvariant_image)


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
def flye() -> Tool:
    # A Python entry point like cutadapt, not like NanoPlot: its imports are
    # its own modules rather than pandas/scipy/plotly, so the default timeout
    # is right. If this ever starts failing the probe on a cold container,
    # SLOW_IMPORT_TIMEOUT_SECONDS is the knob -- but measure before reaching
    # for it, since that constant exists for an import graph Flye does not have.
    return _probe("flye", settings.flye_path, ["--version"])


@lru_cache(maxsize=1)
def miniprot() -> Tool:
    # compleasm's protein aligner, built from source alongside it -- see
    # install-compleasm.sh. Probed separately from compleasm() so a broken
    # miniprot (the more likely failure, since it is the compiled half) shows
    # up as its own row in the tool panel rather than being hidden behind
    # compleasm's own "not found" message.
    return _probe("miniprot", settings.miniprot_path, ["--version"])


@lru_cache(maxsize=1)
def compleasm() -> Tool:
    # `--version` prints "compleasm X.Y.Z" with no import cost worth a slow
    # timeout -- unlike NanoPlot, its own imports are pandas and its own
    # modules, not a plotting stack.
    return _probe("compleasm", settings.compleasm_path, ["--version"])


@lru_cache(maxsize=1)
def ivar() -> Tool:
    # `version`, not `--version`: iVar takes it as a subcommand. Verified
    # against a real installed 1.4.4 binary on 2026-08-05 -- passing
    # --version exits non-zero and would read a working binary as absent.
    return _probe("ivar", settings.ivar_path, ["version"])


@lru_cache(maxsize=1)
def polypolish() -> Tool:
    # `--version` (unlike iVar's subcommand form) -- verified against a real
    # installed 0.7.1 binary on 2026-08-05, which prints "Polypolish 0.7.1"
    # and exits zero.
    #
    # Absent on arm64 by design rather than by accident: upstream ships no
    # linux-aarch64 build and the install script skips it there. The generic
    # "not found on PATH" message from _probe reads as a broken install, so
    # replace it with an explicit architecture note on arm64.
    tool = _probe("polypolish", settings.polypolish_path, ["--version"])
    if tool.error and is_arm64():
        return Tool(
            name="polypolish",
            path=None,
            version=None,
            error=(
                "Polypolish is not available on arm64 / Apple Silicon. "
                "Upstream ships only x86-64 and macOS binaries; there is no "
                "linux-aarch64 build, so BioFlow intentionally skips it."
            ),
        )
    return tool


@lru_cache(maxsize=1)
def ragtag() -> Tool:
    # The binary is `ragtag.py`, not `ragtag` -- a probe looking for `ragtag`
    # on PATH finds nothing and reports a working install as missing, the
    # same shape iVar's `version`-vs-`--version` trap has. Verified against a
    # real installed 2.1.0: `ragtag.py --version` prints "v2.1.0" and exits
    # zero.
    return _probe("ragtag", settings.ragtag_path, ["--version"])


@lru_cache(maxsize=1)
def quast() -> Tool:
    # Verified against a real installed 5.3.0: `quast.py --version` prints
    # "QUAST v5.3.0" and exits zero, alongside a harmless
    # "WARNING: Python locale settings can't be changed" on stderr that
    # `_probe` already tolerates (it reads whichever stream produced
    # something).
    return _probe("quast", settings.quast_path, ["--version"])


@lru_cache(maxsize=1)
def craq() -> Tool:
    # Verified against a real installed 1.10 (2026-08-06): `craq -h` prints
    # "CRAQ Version: 1.10" on stdout but still exits 1 -- `_probe` already
    # tolerates a non-zero exit when the output still looks like a version,
    # the same shape featurecounts' probe relies on. Bare `craq` (no flag)
    # prints only an argument error with no recognizable version string and
    # exits 2, so the flag is still load-bearing: without it the probe would
    # read a working install as absent.
    return _probe("craq", settings.craq_path, ["-h"])


# Debian's `meryl` package is `0~20150903+r2013-9+b1` -- the Celera
# Assembler k-mer suite, an unrelated program that happens to share the
# name. Merqury needs Marbl meryl 1.3+. A probe that merely reported
# whatever version string it found would call that install green and then
# fail at runtime on arguments the binary has never heard of, which is the
# same shape as Debian's BUSCO.
#
# Verified against the real Debian package (2026-08-07): `meryl --version`
# does NOT exit non-zero and does NOT print a dpkg-style version string.
# It exits 0 and prints "Unknown option '--version'." to stdout, because
# Celera meryl's argument parser doesn't recognise `--version` at all and
# treats it like any other bad flag. That output doesn't match
# `_VERSION_PATTERN`, so `_probe` leaves it in `Tool.version` verbatim
# rather than routing it through the exit-code mismatch branch -- an
# earlier version of this check matched on the dpkg version string shape
# (`0~20150903...`), which the binary itself never prints, so it never
# fired. The message text itself is what actually distinguishes the two
# programs.
_CELERA_MERYL_UNKNOWN_OPTION = re.compile(r"^Unknown option ")


@lru_cache(maxsize=1)
def meryl() -> Tool:
    # Verified against a real installed 1.4.2 (2026-08-06): `meryl --version`
    # prints "meryl 1.4.2" and exits zero.
    probed = _probe("meryl", settings.meryl_path, ["--version"])
    if probed.version and _CELERA_MERYL_UNKNOWN_OPTION.match(probed.version):
        return Tool(
            name="meryl",
            path=probed.path,
            version=None,
            error=(
                f"Found meryl at {probed.path}, but it does not understand "
                f"--version ({probed.version!r}) -- this is Debian's Celera "
                f"Assembler k-mer suite, not Marbl meryl. Merqury needs "
                f"Marbl meryl 1.3 or newer (this image installs 1.4.2 at "
                f"/opt/meryl/bin/meryl). Set MERYL_PATH to a Marbl build."
            ),
        )
    return probed


@lru_cache(maxsize=1)
def merqury() -> Tool:
    # merqury.sh prints its usage banner (which carries no version) on a bare
    # call and exits 0, so the version comes from the install directory
    # rather than from the tool. `_probe` with no version args still answers
    # the question that matters -- is it on PATH and executable.
    return _probe("merqury", settings.merqury_path, [])


@lru_cache(maxsize=1)
def gci() -> Tool:
    # GCI.py takes -v/--version via argparse and exits zero.
    return _probe("gci", settings.gci_path, ["--version"])


@lru_cache(maxsize=1)
def winnowmap() -> Tool:
    # Verified against a real build of v2.03 (2026-08-07): `winnowmap
    # --version` prints a bare "2.03" (no tool name prefix, unlike
    # minimap2) and exits zero.
    return _probe("winnowmap", settings.winnowmap_path, ["--version"])


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
        star(),
        samtools(),
        bcftools(),
        bgzip(),
        clair3(),
        deepvariant(),
        flye(),
        miniprot(),
        compleasm(),
        ivar(),
        fasterq_dump(),
        prefetch(),
        datasets(),
        featurecounts(),
        pydeseq2(),
        quast(),
        craq(),
        meryl(),
        merqury(),
        gci(),
        winnowmap(),
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
    ASSEMBLE = "assemble"
    # Tools that judge an assembly rather than produce one -- compleasm,
    # eventually gfastats or a reference-based QC tool. Kept separate from
    # ASSEMBLE rather than folded in: `PipelineType` crosses the API and
    # `PipelineToolSelector.tsx` filters the user's tool picker on it, so a
    # completeness tool declared ASSEMBLE would be offered in the picker
    # headed "an assembler", beside Flye, as something to assemble *with*.
    # See docs/superpowers/specs/2026-08-02-post-assembly-qc-design.md.
    ASSEMBLY_QC = "assembly_qc"
    # Tools that improve, scaffold, polish, or produce an assembly using a
    # reference, draft assembly, or alignment. Kept separate from ASSEMBLE and
    # ASSEMBLY_QC so those picker families do not mix production, improvement,
    # and judgement tools.
    REFERENCE_ASSEMBLY = "reference_assembly"


class Delivery(StrEnum):
    """How a tool reaches this application, per
    docs/superpowers/specs/2026-08-05-optional-tool-delivery-design.md.

    BUNDLED tools are in the backend image and probed by PATH, same as every
    tool here today. ON_DEMAND_IMAGE tools are pinned OCI images, pulled on
    first use and run as sibling containers -- the shape DeepVariant already
    uses, generalized rather than special-cased per tool. There is
    deliberately no third option for installing into a mutable volume: if a
    tool cannot be a pinned image, it stays BUNDLED, trading coverage for one
    delivery mechanism that is atomic, versioned, and reversible.
    """

    BUNDLED = "bundled"
    ON_DEMAND_IMAGE = "on_demand"


class RecommendationLevel(StrEnum):
    RECOMMENDED = "recommended"
    COMPATIBLE = "compatible"


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

    # --- Optional-tool delivery. ---
    # See Delivery's docstring. `image` and `download_bytes` are required
    # together whenever delivery is ON_DEMAND_IMAGE -- enforced by
    # test_every_tool_is_documented, not by the type, so a tool can still be
    # constructed while its entry is being filled in.
    delivery: Delivery = Delivery.BUNDLED
    image: str | None = None  # pinned tag, arch-resolved where relevant
    # What the Install button promises to download. Compressed transfer size,
    # not on-disk size after decompression -- that is the number a user
    # weighing "is my connection up for this" actually wants, and it is what
    # `docker pull`'s own progress output reports against.
    download_bytes: int | None = None

    # --- Tool selection recommendations, keyed on read chemistry. ---
    # Maps a coarse bucket ("short" / "long") to a recommendation level.
    # Absent keys mean no opinion: the tool is not recommended for that
    # read type. The frontend renders a "Recommended" badge for RECOMMENDED
    # and nothing for COMPATIBLE (which is the "works but not first choice"
    # tier).
    recommendations: dict[str, str] = field(default_factory=dict, repr=False)


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
        recommendations={"short": RecommendationLevel.RECOMMENDED.value, "long": RecommendationLevel.COMPATIBLE.value},
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
        recommendations={"short": RecommendationLevel.COMPATIBLE.value, "long": RecommendationLevel.RECOMMENDED.value},
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
        recommendations={"short": RecommendationLevel.COMPATIBLE.value},
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
        recommendations={"short": RecommendationLevel.RECOMMENDED.value},
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
        recommendations={"long": RecommendationLevel.RECOMMENDED.value},
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
            "One of the five aligners an alignment job can select, and the "
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
            "One of the five aligners an alignment job can select. Its index is "
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
            "One of the five aligners an alignment job can select, and the "
            "RNA-seq choice. Like bowtie2 its index comes from a separate "
            "hisat2-build binary this application runs on demand."
        ),
    ),
    "star": ToolMeta(
        pipelines=(PipelineType.ALIGN,),
        one_liner="Splice-aware RNA-seq aligner, fastest but memory-hungry",
        summary=(
            "The reference RNA-seq aligner, and the one most published "
            "pipelines use. It is substantially faster than HISAT2 and finds "
            "novel junctions more sensitively, at the cost of an uncompressed "
            "suffix-array index that must be resident in RAM: roughly 30 GB "
            "for a human genome, against HISAT2's 4 GB. On a machine that "
            "cannot spare that, HISAT2 is the alignment that finishes."
        ),
        strengths=(
            "Splice-aware, with sensitive novel-junction discovery",
            "Faster than other splice-aware aligners on the same hardware",
            "Two-pass mode re-aligns against junctions found in the first pass",
            "Reports per-junction counts and a detailed mapping summary",
            "The conventional choice in published RNA-seq pipelines",
        ),
        homepage="https://github.com/alexdobin/STAR",
        repository="https://github.com/alexdobin/STAR",
        citation="Dobin et al., Bioinformatics 2013",
        citation_url="https://doi.org/10.1093/bioinformatics/bts635",
        license="GPL-3.0-or-later",
        usage=(
            "One of the five aligners an alignment job can select, and the "
            "fast RNA-seq choice. Its index is a directory this application "
            "builds on demand with STAR's own genomeGenerate mode, sizing the "
            "suffix-array and chromosome-bin parameters from the reference "
            "rather than leaving defaults that misbehave on small genomes. "
            "Alignment runs without an annotation file, so junctions are "
            "discovered from the reads rather than read from a GTF."
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
    # No `pipelines`: this is not a card on the tool selector or the Software
    # help page, it is dispatched internally by the storage layer at ingest.
    # `runnable=False` for the same reason -- no job handler branches on it,
    # `cas`/`object_service` call it directly.
    "bgzip": ToolMeta(
        pipelines=(),
        runnable=False,
        one_liner="Block-compressed gzip for FASTQ/FASTA/VCF at ingest",
        summary=(
            "The htslib BGZF compressor. Every stored FASTQ, FASTA and VCF is "
            "bgzip'd at ingest rather than left plain: BGZF is a valid gzip "
            "stream (every reader that accepts .gz accepts it unchanged) but "
            "is also block-seekable, which is what samtools faidx and tabix "
            "need to index a reference or a VCF."
        ),
        strengths=(
            "Ordinary gzip decoders read it unmodified",
            "Block structure makes faidx/tabix indexing possible",
            "Multi-threaded (-@), so compression does not serialize behind one core",
        ),
        homepage="https://www.htslib.org/",
        repository="https://github.com/samtools/htslib",
        citation="Danecek et al., GigaScience 2021",
        citation_url="https://doi.org/10.1093/gigascience/giab008",
        license="MIT",
        usage=(
            "Runs once per ingest on FASTQ, FASTA and VCF content, fused into "
            "the same pass that hashes the file so it costs no extra read. "
            "Falls back to Python's stdlib gzip when this binary is absent, "
            "which trades away block-seekability rather than failing the "
            "ingest -- see docs/superpowers/specs/"
            "2026-08-05-object-compression-design.md."
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
        delivery=Delivery.ON_DEMAND_IMAGE,
        # Read at import time, same as every other module-level use of
        # settings here (probes are cached too) -- this picks up
        # settings.deepvariant_image's own x86-64/arm64 dispatch
        # (default_deepvariant_image in config.py) rather than repeating it.
        image=settings.deepvariant_image,
        # Measured 2026-07-31 pulling google/deepvariant:1.9.0: 2.99 GB
        # compressed transfer (docs/superpowers/specs/
        # 2026-07-31-deepvariant-sidecar-design.md). That is what the Install
        # button should promise, not the 8.83 GB the image occupies on disk
        # once decompressed and unpacked -- a user weighing "is my connection
        # up for this" is asking about the download, not the disk cost.
        download_bytes=2_990_000_000,
    ),
    "flye": ToolMeta(
        pipelines=(PipelineType.ASSEMBLE,),
        one_liner="De novo assembler for long reads",
        summary=(
            "Assembles long reads into contigs without a reference, using "
            "repeat graphs to resolve repetitive regions rather than "
            "collapsing them. Handles Nanopore and PacBio, from error-prone "
            "CLR through HiFi, with an input mode per read accuracy."
        ),
        strengths=(
            "Nanopore and PacBio, including HiFi",
            "Repeat graphs resolve repeats instead of collapsing them",
            "Reports per-contig coverage and circularity, so a finished "
            "bacterial chromosome is visible as one circular contig",
            "Bundles its own polishing, so a draft needs no second tool",
        ),
        homepage="https://github.com/fenderglass/Flye",
        repository="https://github.com/fenderglass/Flye",
        # The single-genome paper. metaFlye (Nat Methods 2020) is the one to
        # cite for metagenomes, which BioFlow does not offer a mode for -- so
        # naming it here would put the wrong reference in a methods section.
        citation=(
            "Kolmogorov M, Yuan J, Lin Y, Pevzner P. Assembly of long "
            "error-prone reads using repeat graphs. Nat Biotechnol. 2019."
        ),
        citation_url="https://doi.org/10.1038/s41587-019-0072-8",
        # Verified against the project's LICENSE file on 2026-08-01: BSD
        # 3-clause, "Copyright (c) 2016, The Regents of the University of
        # California". The README says only "a BSD license", which does not
        # distinguish 2- from 3-clause.
        license="BSD-3-Clause",
        usage=(
            "The assembler for long reads, and the only one installed. The "
            "input mode follows the reads' inferred chemistry, which is what "
            "tells Flye how accurate they are. Produces a contig FASTA that "
            "becomes a reference you can align against, an assembly graph, "
            "and a per-contig table whose coverage and circularity are stored "
            "as facts on the assembly. Short reads are not offered: Flye "
            "cannot assemble them."
        ),
    ),
    "miniprot": ToolMeta(
        pipelines=(PipelineType.ASSEMBLY_QC,),
        one_liner="Protein-to-genome aligner behind compleasm",
        summary=(
            "Aligns protein sequences to a genome assembly, splice-aware. "
            "BioFlow's only use of it is as compleasm's aligner: it maps each "
            "lineage's marker proteins onto the assembly so compleasm can "
            "score which ones were found."
        ),
        strengths=(
            "Splice-aware protein-to-genome alignment",
            "Native SSE2/NEON support -- no arm64 patching needed, unlike "
            "bwa-mem2",
            "Fast enough to make compleasm's speed advantage over BUSCO real",
        ),
        homepage="https://github.com/lh3/miniprot",
        repository="https://github.com/lh3/miniprot",
        citation=(
            "Li H. Protein-to-genome alignment with miniprot. "
            "Bioinformatics. 2023."
        ),
        citation_url="https://doi.org/10.1093/bioinformatics/btad014",
        # Verified against the upstream repository's LICENSE.txt on
        # 2026-08-02.
        license="MIT",
        # Not surfaced on its own card: nothing in BioFlow dispatches to
        # miniprot directly, only through compleasm.
        runnable=False,
        usage=(
            "Invoked by compleasm, never directly. BioFlow does not expose a "
            "standalone protein-alignment pipeline."
        ),
    ),
    "compleasm": ToolMeta(
        pipelines=(PipelineType.ASSEMBLY_QC,),
        one_liner="Fast BUSCO reimplementation for assembly completeness",
        summary=(
            "Scores what fraction of a lineage-specific set of single-copy "
            "orthologs (BUSCOs) can be found in an assembly, split into "
            "single-copy, duplicated, fragmented and missing. A "
            "miniprot-based reimplementation of BUSCO: 10-20x faster, and "
            "it recovers some BUSCOs that BUSCO's own metaeuk step misses."
        ),
        strengths=(
            "10-20x faster than BUSCO on the same lineage set",
            "Recovers BUSCOs metaeuk calls missing",
            "No separate eukaryotic gene-finder to install or forget: "
            "alignment is miniprot for every lineage",
        ),
        homepage="https://github.com/huangnengCSU/compleasm",
        repository="https://github.com/huangnengCSU/compleasm",
        citation=(
            "Huang N, Li H. compleasm: a faster and more accurate "
            "reimplementation of BUSCO. Bioinformatics. 2023."
        ),
        citation_url="https://doi.org/10.1093/bioinformatics/btad595",
        # Verified against the upstream repository's LICENSE (Apache-2.0) and
        # bundled LICENSE-BUSCO (MIT, for the inherited BUSCO lineage-scoring
        # logic) on 2026-08-02.
        license="Apache-2.0",
        usage=(
            "Scores assembly completeness against a lineage-specific "
            "ortholog set chosen from the assembly's organism metadata, not "
            "auto-detected -- autolineage would download several lineage "
            "datasets to decide between them, which is the expensive way to "
            "answer a question BioFlow mostly already knows. Runs as its "
            "own job, separate from contiguity, since a bacterial run is "
            "minutes and a vertebrate one can be hours. Records lineage and "
            "OrthoDB version alongside the four percentages, since a score "
            "from one version is not comparable to a score from another."
        ),
    ),
    "quast": ToolMeta(
        pipelines=(PipelineType.ASSEMBLY_QC,),
        one_liner="Reference-based misassembly detection",
        summary=(
            "Aligns a draft assembly against a related reference and reports "
            "structural disagreements -- relocations, translocations and "
            "inversions -- that neither contiguity nor completeness can see. "
            "A chimeric assembly that joins two chromosomes into one contig "
            "scores better on N50 and identically on completeness, since "
            "every gene is still present, just in the wrong place; this is "
            "the only BioFlow check that catches that."
        ),
        strengths=(
            "Reports per-breakpoint coordinates and error type "
            "(relocation/translocation/inversion), not just a count",
            "Genome fraction and duplication ratio, alongside the "
            "misassembly breakdown, from one run",
            "No compilation needed -- it prefers an installed minimap2 "
            "over its own bundled copy",
        ),
        homepage="https://quast.sourceforge.net/",
        repository="https://github.com/ablab/quast",
        citation=(
            "Alla Mikheenko, Vladislav Saveliev, Pascal Hirsch, Alexey "
            "Gurevich, WebQUAST: online evaluation of genome assemblies. "
            "Nucleic Acids Research. 2023;51(W1):W601-W606."
        ),
        citation_url="https://doi.org/10.1093/nar/gkad406",
        # From the repository's own LICENSE.txt, checked 2026-08-05: GNU
        # General Public License, Version 2.
        license="GPL-2.0",
        usage=(
            "Runs quast.py in reference-based mode only, against a "
            "reference the user picks -- no --gene-finding, "
            "--rna-finding or --conserved-genes-finding, and no de novo "
            "mode. The input is linked under a fixed name and passed with "
            "a fixed --l label rather than the object's own name: QUAST "
            "sanitizes contig names but not the assembly label, and the "
            "label is otherwise taken from the input filename. Stores only "
            "the reference-derived numbers as facts -- misassembly counts "
            "and types, genome fraction, duplication ratio, mismatches and "
            "indels per 100 kbp -- alongside which reference and "
            "--min-contig cutoff produced them. Contiguity numbers QUAST "
            "also reports (N50, L50, total length, ...) are not stored "
            "here, since BioFlow already computes those for every FASTA "
            "at ingest and a second code path with a different cutoff "
            "would eventually disagree with the first."
        ),
    ),
    "craq": ToolMeta(
        pipelines=(PipelineType.ASSEMBLY_QC,),
        one_liner="Reference-free assembly error detection from read clipping",
        summary=(
            "Finds positions where reads align to the assembly only "
            "partially -- clipped alignments pile up where the assembly is "
            "wrong. Reports small-scale regional errors (CRE) and "
            "large-scale structural errors (CSE), and separates both from "
            "their heterozygous-variant lookalikes (CRH/CSH), which is what "
            "keeps a diploid assembly's real heterozygosity from reading as "
            "misassembly. Needs no reference genome -- only the reads, "
            "aligned back to the assembly they built."
        ),
        strengths=(
            "Reference-free: catches errors in organisms with no related "
            "reference assembly, where QUAST cannot run at all",
            "Separates true misassemblies from heterozygous variants "
            "rather than counting both as errors",
            "Published AQI quality bands (>90 reference, 80-90 high, "
            "60-80 draft, <60 low) for a directly interpretable score",
        ),
        homepage="https://github.com/JiaoLaboratory/CRAQ",
        repository="https://github.com/JiaoLaboratory/CRAQ",
        citation=(
            "Li K, Xu P, Wang J, Yi X, Jiao Y. Identification of errors in "
            "draft genome assemblies at single-nucleotide resolution for "
            "quality assessment and improvement. Nature Communications. "
            "2023;14:6556."
        ),
        citation_url="https://doi.org/10.1038/s41467-023-42336-w",
        # From the repository's own metadata, checked 2026-08-06 via
        # `gh api repos/JiaoLaboratory/CRAQ` -> spdx_id: MIT.
        license="MIT",
        usage=(
            "Runs against sorted BAMs BioFlow's own align pipeline "
            "produced, never raw FASTQ -- upstream recommends pre-made "
            "alignments, and it keeps a second aligner from running "
            "hidden inside a QC job. Short-read BAMs are passed as -ngs "
            "and long-read as -sms, decided from the reads' recorded "
            "chemistry rather than guessed. Circos plotting (-pl) is never "
            "enabled, so no pycircos dependency is installed and no "
            "CRAQ-generated document is served. Chimera breaking (-b) is "
            "off unless the user opts in, and its corrected FASTA is "
            "ingested as a new object rather than replacing the assembly "
            "it came from."
        ),
    ),
    "meryl": ToolMeta(
        pipelines=(PipelineType.ASSEMBLY_QC,),
        one_liner="K-mer database builder used by Merqury for QV assessment",
        summary=(
            "Builds and manipulates k-mer count databases. Merqury uses it "
            "to build a k-mer database from a read set and another from an "
            "assembly, then compares the two -- k-mers in the assembly but "
            "absent from the reads are evidence of base errors, which is "
            "how Merqury derives its reference-free QV score."
        ),
        strengths=(
            "Reference-free: needs only the reads an assembly was built "
            "from, no related reference genome",
            "The k-mer database it builds from a read set is reusable "
            "across every assembly evaluated against those reads",
        ),
        homepage="https://github.com/marbl/meryl",
        repository="https://github.com/marbl/meryl",
        citation=(
            "Rhie, A., Walenz, B.P., Koren, S. et al. Merqury: reference-free "
            "quality, completeness, and phasing assessment for genome "
            "assemblies. Genome Biol 21, 245 (2020)."
        ),
        citation_url="https://doi.org/10.1186/s13059-020-02134-9",
        # No top-level LICENSE file in the repo; its README.licenses states
        # the code is a "United States Government Work" (public domain),
        # the same notice Merqury's own LICENSE carries, with a note that
        # individual contributions may carry other licenses per source file.
        # Verified 2026-08-06 via `gh api repos/marbl/meryl/contents/README.licenses`.
        license="Public domain (US Government work); some files under other licenses",
        usage=(
            "Builds the k-mer databases Merqury compares. The database "
            "built from a read set is cached as a sidecar on that read "
            "object and reused across assemblies; the one built from an "
            "assembly is rebuilt per run and discarded. Note this is Marbl "
            "meryl, not Debian's same-named Celera Assembler k-mer suite."
        ),
        runnable=True,
    ),
    "merqury": ToolMeta(
        pipelines=(PipelineType.ASSEMBLY_QC,),
        one_liner="Reference-free k-mer QV and completeness assessment",
        summary=(
            "Scores an assembly's base-level accuracy (QV) and k-mer "
            "completeness against the reads it was built from, with no "
            "reference genome required, and renders copy-number spectra "
            "plots. A high QV means few k-mers appear in the assembly that "
            "are absent from the reads -- evidence of few base errors."
        ),
        strengths=(
            "Reference-free: works for organisms with no related reference "
            "assembly to compare against",
            "A single QV number that is directly comparable across "
            "assemblies of the same organism",
            "Spectra-cn plots show copy-number errors a single QV score "
            "cannot distinguish",
        ),
        homepage="https://github.com/marbl/merqury",
        repository="https://github.com/marbl/merqury",
        citation=(
            "Rhie, A., Walenz, B.P., Koren, S. et al. Merqury: reference-free "
            "quality, completeness, and phasing assessment for genome "
            "assemblies. Genome Biol 21, 245 (2020)."
        ),
        citation_url="https://doi.org/10.1186/s13059-020-02134-9",
        # From the repository's own LICENSE file, checked 2026-08-06 via
        # `gh api repos/marbl/merqury/contents/LICENSE`: public-domain notice,
        # "United States Government Work" under the US Copyright Act.
        license="Public domain (US Government work)",
        usage=(
            "Scores an assembly's base-level accuracy (QV) and k-mer "
            "completeness against the reads it was built from, with no "
            "reference genome, and renders the copy-number spectra plots. "
            "Trio and hap-mer modes are never used -- BioFlow has no "
            "parental read-set concept."
        ),
        runnable=True,
    ),
    "gci": ToolMeta(
        pipelines=(PipelineType.ASSEMBLY_QC,),
        one_liner="Assembly continuity scoring from long-read alignment gaps",
        summary=(
            "Scores how well long reads support an assembly's continuity by "
            "finding regions with no read coverage, or coverage that "
            "contradicts a contiguous assembly (clipped or split "
            "alignments piling up at the same position). Reports both "
            "per-contig and whole-assembly continuity scores rather than "
            "a single pass/fail number."
        ),
        strengths=(
            "Reference-free, like CRAQ -- scores continuity from the "
            "reads alone, with no related assembly required",
            "Works from a single alignment file (BAM or PAF), so it adds "
            "no new aligner dependency on top of what BioFlow already "
            "runs for long reads",
            "Flags unsupported regions with their coordinates, not just "
            "an aggregate score, so a low score can be traced to specific "
            "contigs",
        ),
        homepage="https://github.com/yeeus/GCI",
        repository="https://github.com/yeeus/GCI",
        citation=(
            "Chen, Quanyu, et al. GCI: a continuity inspector for complete "
            "genome assembly. Bioinformatics 40.11 (2024): btae633."
        ),
        citation_url="https://doi.org/10.1093/bioinformatics/btae633",
        # From the repository's own metadata, checked 2026-08-07 via
        # `gh api repos/yeeus/GCI` -> license.spdx_id: MIT.
        license="MIT",
        usage=(
            "Scores assembly continuity from long reads aligned back to the "
            "assembly. Runs against BioFlow-produced minimap2 alignments "
            "alone, or minimap2 paired with winnowmap when both are "
            "available for the same reads -- upstream recommends the pair "
            "for higher sensitivity in repetitive regions -- and the "
            "aligners actually used are recorded with the score. "
            "Whole-assembly only; regions mode and trio binning are not "
            "used."
        ),
    ),
    "winnowmap": ToolMeta(
        pipelines=(PipelineType.ASSEMBLY_QC,),
        one_liner="Repeat-aware long-read aligner, GCI's second cross-check aligner",
        summary=(
            "Aligns long reads to an assembly using minimizers weighted by "
            "how repetitive they are, computed by meryl -- so highly "
            "repetitive k-mers contribute less to seeding than they would "
            "under a plain minimizer scheme. This is specifically tuned "
            "for repetitive regions, where a uniform scheme like "
            "minimap2's over-samples common k-mers and mis-seeds."
        ),
        strengths=(
            "Cross-checks minimap2's alignments in repetitive regions, "
            "which is what GCI's own FAQ recommends pairing two aligners "
            "for",
            "Shares minimap2's calling convention (presets, -a, -R), so it "
            "slots into the same align pipeline as every other aligner "
            "here rather than needing a bespoke run path",
        ),
        homepage="https://github.com/marbl/Winnowmap",
        repository="https://github.com/marbl/Winnowmap",
        citation=(
            "Jain, C., Rhie, A., Hansen, N.F. et al. Long-read mapping to "
            "repetitive reference sequences using Winnowmap2. Nat Methods "
            "19, 705-710 (2022)."
        ),
        citation_url="https://doi.org/10.1038/s41592-022-01457-8",
        # GitHub reports license.spdx_id "NOASSERTION" for this repo, which
        # is GitHub failing to classify the file, not an absent license.
        # Verified 2026-08-07 via `gh api repos/marbl/Winnowmap/contents/LICENSE`:
        # an NIH/NHGRI public-domain dedication ("This software is freely
        # available to the public for use without a copyright notice"),
        # noting the codebase is a joint work whose individual contributions
        # may carry their own licenses per source file.
        license="Public domain (NIH/NHGRI); some files under other licenses",
        usage=(
            "Runs as a second long-read aligner alongside minimap2, purely "
            "to give GCI's continuity score a repeat-aware cross-check. "
            "Ships no binary release -- built from source at image build "
            "time -- and needs a repetitive-k-mer file meryl builds per "
            "reference (`meryl count` then `meryl print greater-than`), "
            "modeled as this aligner's index. No short-read mode, no "
            "annotation-aware mode; never chosen as the default aligner for "
            "a general alignment, only picked explicitly from the align "
            "dialog."
        ),
    ),
    "ragtag": ToolMeta(
        pipelines=(PipelineType.REFERENCE_ASSEMBLY,),
        one_liner="Reference-guided assembly scaffolding",
        summary=(
            "Orders and orients a draft assembly's contigs by aligning them "
            "against a related reference assembly, producing chromosome-scale "
            "scaffolds without Hi-C data. Scaffolds are named after the "
            "reference's own sequences, so the result inherits the "
            "reference's arrangement -- a real structural difference between "
            "the sample and the reference will not appear in the output."
        ),
        strengths=(
            "The cheapest route from contigs to chromosome-scale sequence "
            "when a reasonable reference already exists",
            "Reports a per-contig placement confidence rather than a single "
            "pass/fail result",
            "Carries unplaced contigs through into the output instead of "
            "silently dropping them",
        ),
        homepage="https://github.com/malonge/RagTag",
        repository="https://github.com/malonge/RagTag",
        citation=(
            "Alonge M, Lebeigle L, Kirsche M, Jenike K, Ou S, Aganezov S, "
            "Wang X, Lippman ZB, Schatz MC, Soyk S. Automated assembly "
            "scaffolding using RagTag elevates a new tomato system for "
            "high-throughput genome editing. Genome Biology. 2022;23:258."
        ),
        citation_url="https://doi.org/10.1186/s13059-022-02823-7",
        # From the repository's own LICENSE, checked 2026-08-05: "MIT
        # License / Copyright (c) 2021 Michael Alonge".
        license="MIT",
        usage=(
            "Orders and orients a draft assembly's contigs against a related "
            "reference assembly using ragtag.py scaffold, which aligns the "
            "two internally with minimap2 at a divergence preset the user "
            "chooses. The scaffolded assembly is stored as a new object "
            "beside the draft, with the reference it was ordered against and "
            "per-contig placement confidence recorded as facts, since "
            "scaffold structure is inferred from the reference rather than "
            "observed in the sample. Unplaced contigs are carried through "
            "into the output rather than dropped. Upstream also credits the "
            "earlier RaGOO method this tool superseded (Alonge et al., "
            "Genome Biology 2019, doi:10.1186/s13059-019-1829-6)."
        ),
    ),
    "polypolish": ToolMeta(
        pipelines=(PipelineType.REFERENCE_ASSEMBLY,),
        one_liner="Short-read polishing of long-read assemblies",
        summary=(
            "Corrects residual base errors in a long-read assembly using "
            "high-accuracy short reads. Unlike older polishers it reads "
            "alignments in which each read is mapped to every location it "
            "matches, not just its best one, so it declines to change "
            "positions where those locations disagree -- which is what makes "
            "it safe to run on repetitive regions."
        ),
        strengths=(
            "Uses all-alignment input, so repeats are not mis-corrected "
            "toward whichever copy a best-alignment mapper happened to pick",
            "Does not degrade an already-accurate assembly: the common "
            "failure of naive short-read polishing is introducing errors, "
            "not missing them",
            "A single static binary with a predictable memory footprint",
        ),
        homepage="https://github.com/rrwick/Polypolish",
        repository="https://github.com/rrwick/Polypolish",
        citation=(
            "Wick RR, Holt KE. Polypolish: Short-read polishing of long-read "
            "bacterial genome assemblies. PLOS Computational Biology. "
            "2022;18(1):e1009802."
        ),
        citation_url="https://doi.org/10.1371/journal.pcbi.1009802",
        # From the repository's own LICENSE and its GitHub-reported SPDX id,
        # checked 2026-08-05 rather than recalled.
        license="GPL-3.0",
        usage=(
            "Corrects residual base errors in a draft assembly using short "
            "reads. BioFlow aligns the reads to the draft itself with "
            "bwa-mem2, reporting every location each read matches rather "
            "than only its best one, because that all-alignment input is "
            "what lets Polypolish leave ambiguous repeat positions alone. "
            "Paired reads are additionally passed through Polypolish's own "
            "insert-size filter. Below 25x estimated depth it runs in "
            "careful mode, which stops it correcting repeats at all rather "
            "than acting on thin evidence. The polished assembly is stored "
            "as a new object beside the draft, never replacing it, since the "
            "comparison between the two is the evidence that polishing "
            "helped. Upstream also asks that users of 0.6.0 and later cite "
            "Bouras et al., Microbial Genomics 2024 "
            "(doi:10.1099/mgen.0.001254) alongside the paper above."
        ),
    ),
    "ivar": ToolMeta(
        pipelines=(PipelineType.REFERENCE_ASSEMBLY,),
        one_liner="Amplicon primer trimming and viral consensus calling",
        summary=(
            "Trims amplicon primer sequences from aligned reads and calls a "
            "consensus sequence from the resulting pileup. Built for viral "
            "amplicon protocols such as ARTIC/PrimalSeq, where primer bases "
            "are synthetic rather than sample sequence and would otherwise "
            "manufacture false reference-matching calls at amplicon overlaps."
        ),
        strengths=(
            "Primer-aware trimming from a BED scheme, not just adapter trimming",
            "Consensus calling with explicit quality, frequency and depth "
            "thresholds recorded per run",
            "Purpose-built for viral/amplicon data rather than adapted "
            "general-purpose variant-caller output",
        ),
        homepage="https://andersen-lab.github.io/ivar/html/",
        repository="https://github.com/andersen-lab/ivar",
        citation=(
            "Grubaugh ND, Gangavarapu K, Quick J, et al. An amplicon-based "
            "sequencing framework for accurately measuring intrahost virus "
            "diversity using PrimalSeq and iVar. Genome Biology. "
            "2019;20:8."
        ),
        citation_url="https://doi.org/10.1186/s13059-018-1618-7",
        # From Debian's /usr/share/doc/ivar/copyright for ivar
        # 1.4.4+dfsg-1, the build actually in this image (verified
        # 2026-08-05): "Copyright: 2018-2020 Nathan D. Grubaugh, Karthik
        # Gangavarapu / License: GPL-3".
        license="GPL-3.0",
        usage=(
            "Trims primer sequences from a BAM aligned against a viral or "
            "amplicon reference using a user-supplied primer BED, then calls "
            "a consensus sequence from the trimmed, sorted pileup. Primer "
            "trimming is skipped -- not refused -- when no primer BED is "
            "supplied, for non-amplicon viral alignments such as "
            "metagenomic or bait-capture data. The consensus is stored as a "
            "new reference object, with the quality/frequency/depth "
            "thresholds and N-count recorded as facts, since a consensus is "
            "meaningless without knowing what thresholds produced it."
        ),
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
            "recommendations": {},
            # Same reasoning as `runnable` above: a tool with no entry here
            # has no delivery story either, so it defaults to the same
            # BUNDLED/absent shape `ToolMeta`'s own fields default to.
            "delivery": Delivery.BUNDLED,
            "image": None,
            "download_bytes": None,
        }
    )
    return {
        **tool.as_dict(),
        **meta_dict,
        "pipelines": [p.value for p in meta_dict["pipelines"]],
        "strengths": list(meta_dict["strengths"]),
        "delivery": meta_dict["delivery"].value,
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
    star.cache_clear()
    bowtie2_build.cache_clear()
    hisat2_build.cache_clear()
    samtools.cache_clear()
    bcftools.cache_clear()
    bcftools_csq.cache_clear()
    bgzip.cache_clear()
    clair3.cache_clear()
    deepvariant.cache_clear()
    flye.cache_clear()
    miniprot.cache_clear()
    compleasm.cache_clear()
    ivar.cache_clear()
    fasterq_dump.cache_clear()
    prefetch.cache_clear()
    datasets.cache_clear()
    featurecounts.cache_clear()
    pydeseq2.cache_clear()
    quast.cache_clear()
    craq.cache_clear()
    meryl.cache_clear()
    merqury.cache_clear()
    gci.cache_clear()
    winnowmap.cache_clear()
