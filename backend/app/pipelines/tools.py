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
            text=True,
            timeout=VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return Tool(name=name, path=resolved, version=None, error=str(e))

    # fastp writes its version to stderr, FastQC to stdout. Take whichever
    # produced something rather than guessing per tool.
    raw = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    return Tool(name=name, path=resolved, version=_clean_version(raw) or None)


def _clean_version(raw: str) -> str:
    """First line, trimmed of the tool's own name.

    `fastp 0.24.0` and `FastQC v0.12.1` both become a bare version, so the UI
    can label them consistently.
    """
    first = raw.splitlines()[0].strip() if raw else ""
    match = re.search(r"v?(\d+\.\d+(?:\.\d+)?)", first)
    return match.group(1) if match else first


@lru_cache(maxsize=1)
def fastp() -> Tool:
    return _probe("fastp", settings.fastp_path, ["--version"])


@lru_cache(maxsize=1)
def fastqc() -> Tool:
    return _probe("fastqc", settings.fastqc_path, ["--version"])


def all_tools() -> list[Tool]:
    return [fastp(), fastqc()]


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
