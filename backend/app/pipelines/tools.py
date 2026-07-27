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
def bwa_mem2() -> Tool:
    # bwa-mem2 has no --version flag: it prints usage, including the version,
    # and exits non-zero. `_probe` ignores the exit code and reads whichever
    # stream produced output, so the usage text is what gets parsed.
    return _probe("bwa-mem2", settings.bwa_mem2_path, ["version"])


@lru_cache(maxsize=1)
def minimap2() -> Tool:
    return _probe("minimap2", settings.minimap2_path, ["--version"])


@lru_cache(maxsize=1)
def samtools() -> Tool:
    return _probe("samtools", settings.samtools_path, ["--version"])


def all_tools() -> list[Tool]:
    return [fastp(), fastqc(), bwa_mem2(), minimap2(), samtools()]


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
    bwa_mem2.cache_clear()
    minimap2.cache_clear()
    samtools.cache_clear()
