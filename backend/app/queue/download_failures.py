"""Sorting a failed download into "retry" and "never going to work".

Shared by the SRA and assembly download handlers, whose bias is the same and
is the opposite of the pipeline handlers': a fastp failure is almost always
the input, while a download failure is almost always the network. Retryable
is therefore the default, and a genuinely permanent failure still stops after
the handler's attempt budget.
"""

from pathlib import Path

from app.errors import PermanentError, RetryableError
from app.queue.pipeline_handlers import _log_tail

# Errors that mean "ask again later" rather than "this will never work". NCBI
# is rate-limited and intermittently unavailable, and burning the attempt
# budget on a transient 503 would fail a download a retry would complete.
RETRYABLE_PATTERNS = (
    "connection",
    "timeout",
    "timed out",
    "network",
    "temporarily",
    "503",
    "502",
    "429",
    "try again",
)


def classify_failure(
    code: int, log_path: Path, accession: str, *, tool: str
) -> Exception:
    """The exception a non-zero download exit deserves."""
    tail = _log_tail(log_path)
    detail = f"{tool} exited {code} for {accession}"
    if tail:
        detail = f"{detail}: {tail}"

    lowered = tail.lower()

    # A retracted or mistyped accession will fail identically forever, so it
    # must not consume the whole attempt budget.
    if "not found" in lowered or "does not exist" in lowered or "invalid" in lowered:
        return PermanentError(detail, details={"accession": accession})

    if "disk" in lowered and ("full" in lowered or "space" in lowered):
        return PermanentError(detail, details={"accession": accession})

    if code == 137:
        return RetryableError(f"{detail} (killed, most likely out of memory)")

    if any(pattern in lowered for pattern in RETRYABLE_PATTERNS):
        return RetryableError(detail)

    return RetryableError(detail)
