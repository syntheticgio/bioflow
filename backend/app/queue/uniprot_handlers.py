"""Downloading protein sequences from UniProt.

Sibling to `ncbi_assembly_handlers`, and deliberately much smaller. That module
is built around shelling out to a binary and guarding a multi-gigabyte
transfer; none of that applies here, so this has no subprocess mode, no
lease extension, no disk pre-flight, no extraction factor, and no archive
handling. A yeast proteome is 3.9 MB and human reviewed is 13.7 MB.

THREAD rather than ASYNC: the body is a blocking urllib call, and the
executor is responsible for keeping blocking work off the event loop. An
ASYNC handler doing this would stall the heartbeat and expire its own lease.

Imported by `handlers.py` for the `@handler` registration side effects.
"""

import gzip
import urllib.error
import urllib.request
from pathlib import Path

from app.errors import PermanentError, RetryableError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.queue.pipeline_handlers import _prepare_workdir
from app.queue.registry import HandlerMode, JobContext, handler

log = get_logger(__name__)

# Generous: a large proteome stream is a single request that can take a
# while to start, but nothing here runs for minutes the way an assembly
# transfer does.
_TIMEOUT_SECONDS = 300.0


def _fetch(url: str, *, timeout: float = _TIMEOUT_SECONDS) -> tuple[bytes, dict]:
    """The transport, isolated so tests can replace it without a network."""
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read(), dict(response.headers)


@handler(
    "download_uniprot",
    # THREAD, not SUBPROCESS: there is no binary. Not ASYNC either, because
    # the urllib call blocks.
    mode=HandlerMode.THREAD,
    # USER_INTERACTIVE for the same reason as the other downloads: someone
    # clicked and is watching for the file, and the work waits on UniProt
    # rather than competing with alignments for CPU.
    job_class=JobClass.USER_INTERACTIVE,
    resources=JobResources(cpu=1, mem_mb=256, io=IoClass.LIGHT),
    # Matches the other downloads: a failure is usually the network, and the
    # third attempt genuinely succeeds often enough to be worth it.
    max_attempts=3,
)
def download_uniprot(ctx: JobContext) -> dict:
    """Fetch a FASTA of proteins. The ingest happens in the applier.

    Synchronous: THREAD runs this off the event loop, so the body must not
    await and cannot touch the database. It stages one file under tmp/ and
    returns a description for `_apply_uniprot_download` to persist.

    Idempotent by construction -- each attempt gets a fresh scratch directory
    and rewrites the file whole, so a retry after a partial transfer starts
    clean rather than appending to a truncated FASTA.
    """
    from app.metadata import uniprot

    query = (ctx.payload.get("query") or "").strip()
    if not query:
        raise PermanentError("download_uniprot requires a 'query'")

    project_id = ctx.payload.get("project_id")
    if not project_id:
        raise PermanentError("download_uniprot requires a 'project_id'")

    filename = ctx.payload.get("filename") or "uniprot.fasta"

    work = _prepare_workdir(ctx, kind="uniprot_download")

    ctx.check_cancel()

    ctx.progress(phase="downloading", pct=0.1, message="fetching from UniProt")
    try:
        body, headers = _fetch(uniprot.stream_url(query))
    except urllib.error.HTTPError as exc:
        # 4xx means the request itself is wrong -- a malformed query, an
        # invalid accession -- and no number of retries changes that.
        # Measured: UniProt answers all three with 400 in about a second, so
        # retrying would spend up to three 300-second timeouts to rediscover
        # what the first response already said. `ncbi_assembly_handlers` draws the
        # same line via `download_failures.classify_failure`.
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace").strip()[:200]
        except Exception:  # noqa: BLE001 - the body is a bonus, not a promise
            pass
        if 400 <= exc.code < 500:
            raise PermanentError(
                f"UniProt rejected this request ({exc.code}): {detail or exc.reason}",
                details={"status": exc.code, "query": query},
            ) from exc
        raise RetryableError(
            f"UniProt returned {exc.code}: {detail or exc.reason}"
        ) from exc
    except Exception as exc:
        # Network failures are the common case and retrying genuinely helps.
        raise RetryableError(f"UniProt request failed: {exc}") from exc

    ctx.check_cancel()

    ctx.progress(phase="writing", pct=0.8, message="writing sequences")
    text = _decode(body)

    # Counted from what arrived rather than trusted from the request.
    # `X-Total-Results` and the delivered record count differ slightly --
    # human reviewed reported 20,416 and delivered 20,427 -- so this is the
    # only honest number, and it also catches an HTML error page, which has
    # no '>' lines at all.
    protein_count = sum(1 for line in text.splitlines() if line.startswith(">"))
    if protein_count == 0:
        raise RetryableError(
            "UniProt returned no sequences for this request. The service may "
            "be busy; this will be retried."
        )

    target = work / filename
    target.write_text(text)

    release = headers.get("X-UniProt-Release")

    ctx.progress(phase="done", pct=1.0, message=f"downloaded {protein_count} proteins")
    log.info(
        "uniprot_download_finished",
        job_id=ctx.job_id,
        query=query,
        proteins=protein_count,
        release=release,
    )

    return {
        "staged": [{"path": str(target.resolve()), "name": filename}],
        "protein_count": protein_count,
        "release": release,
        "query": query,
        "proteome_id": ctx.payload.get("proteome_id"),
        "accessions": ctx.payload.get("accessions") or [],
        "reviewed_only": bool(ctx.payload.get("reviewed_only")),
        "organism": ctx.payload.get("organism"),
        "project_id": project_id,
        "job_id": ctx.job_id,
        "staging_dir": str(work),
    }


def _decode(body: bytes) -> str:
    """The response text, gzipped or not.

    `compressed=true` is a request rather than a guarantee -- a proxy may
    decompress in transit -- so both forms are handled instead of assuming.
    """
    try:
        return gzip.decompress(body).decode("utf-8", "replace")
    except (OSError, EOFError):
        return body.decode("utf-8", "replace")
