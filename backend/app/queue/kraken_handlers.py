"""Downloading a Kraken2 classification database.

Modelled on `lineage_handlers`: fetches reference data shared across every
project, not something derived from one object.  There is no applier -- a
successful run leaves files under `settings.kraken_dbs_dir / <key>` and
nothing else changes state; `launch_classify_reads` checks presence via
`kraken_db_registry.db_present` and chains behind this job when absent.

Unlike compleasm, Kraken2 has no self-managing downloader, so integrity is
this handler's own job: verify the tarball's md5 against the registry,
extract into `<key>.partial`, and rename into place only on success -- a
killed or corrupt download never half-presents (spec K2-N3).

There is no established chunked-streaming-with-progress HTTP helper
elsewhere in this repo to reuse: `uniprot_handlers._fetch` reads its whole
(MB-scale) response into memory in one urllib call, and
`ncbi_assembly_handlers`/`lineage_handlers` both shell out to a vendor CLI
instead of speaking HTTP directly. A multi-gigabyte k2 tarball needs to be
streamed to disk rather than buffered, so this handler does that itself with
urllib, matching the transport uniprot already uses for actual HTTP.

Only the download handler lives here for now. A later `classify_reads`
handler (spec Task 7) is a separate `@handler`-decorated function appended
below `download_kraken_db`; the module docstring and imports above are
written to be shared by both rather than scoped to only this one.

Imported by `handlers.py` for the `@handler` registration side effects.
"""

import hashlib
import shutil
import tarfile
import urllib.request
from pathlib import Path

from app.config import settings
from app.errors import PermanentError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.pipelines.kraken_db_registry import KRAKEN_DBS
from app.queue.registry import HandlerMode, JobContext, handler

log = get_logger(__name__)

_DOWNLOAD_LEASE_SECONDS = 2 * 3600  # 7.5 GB on a slow line takes a while


def verify_md5(tarball: Path, expected: str) -> None:
    """Raise PermanentError when the tarball does not match the registry.

    Permanent rather than retryable on its own: the *job* retries by
    re-downloading (max_attempts=3), but a mismatched file must never be
    extracted, and the message must say which database and why.
    """
    h = hashlib.md5()
    with tarball.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    got = h.hexdigest()
    if got != expected:
        raise PermanentError(
            f"downloaded database failed md5 verification "
            f"(expected {expected}, got {got}) -- corrupt or altered download"
        )


def extract_and_promote(tarball: Path, final_dir: Path) -> None:
    """Extract into `<final>.partial`, rename to `final_dir` on success.

    The rename is the commit point: `db_present()` reads the final path, so
    an interrupted extraction is invisible to every consumer.  The k2
    tarballs place their .k2d files at the archive root.
    """
    partial = final_dir.parent / (final_dir.name + ".partial")
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir(parents=True)
    try:
        with tarfile.open(tarball, "r:gz") as tf:
            tf.extractall(partial, filter="data")
        if final_dir.exists():
            shutil.rmtree(final_dir)
        partial.rename(final_dir)
    except Exception:
        shutil.rmtree(partial, ignore_errors=True)
        raise


@handler(
    "download_kraken_db",
    mode=HandlerMode.SUBPROCESS,
    # USER_INTERACTIVE: someone pressed the classify card and is waiting,
    # the same reasoning download_lineage gives.
    job_class=JobClass.USER_INTERACTIVE,
    resources=JobResources(cpu=1, mem_mb=512, io=IoClass.HEAVY),
    max_attempts=3,
)
def download_kraken_db(ctx: JobContext) -> dict:
    """Fetch one classification database into the shared store.

    Idempotent: an already-present database returns immediately, so the
    dedup collapse in `launch_kraken_db_download` plus this check means a
    re-run is a fast no-op rather than a duplicate 7.5 GB download.
    """
    from app.pipelines.kraken_db_registry import db_present

    key = (ctx.payload.get("db_key") or "").strip()
    spec = KRAKEN_DBS.get(key)
    if spec is None:
        raise PermanentError(f"unknown kraken database {key!r}")

    if db_present(key):
        return {"db_key": key, "already_present": True}

    settings.kraken_dbs_dir.mkdir(parents=True, exist_ok=True)
    tarball = settings.kraken_dbs_dir / f"{key}.tar.gz.partial"

    ctx.progress(phase="downloading", pct=None, message=f"downloading {spec.label}")
    ctx.extend_lease(_DOWNLOAD_LEASE_SECONDS)
    log.info("kraken_db_download_started", job_id=ctx.job_id, db_key=key)

    try:
        with urllib.request.urlopen(spec.url, timeout=60) as resp, tarball.open("wb") as out:
            copied = 0
            while chunk := resp.read(1 << 20):
                out.write(chunk)
                copied += len(chunk)
                if spec.download_bytes:
                    ctx.progress(
                        phase="downloading",
                        pct=min(copied / spec.download_bytes, 0.99),
                        message=f"downloading {spec.label}",
                    )
    except Exception:
        tarball.unlink(missing_ok=True)
        raise  # retryable: the usual failure is the network

    try:
        ctx.progress(phase="verifying", pct=None, message="verifying checksum")
        verify_md5(tarball, spec.md5)
        ctx.progress(phase="extracting", pct=None, message="extracting database")
        extract_and_promote(tarball, settings.kraken_dbs_dir / key)
    finally:
        tarball.unlink(missing_ok=True)

    if not db_present(key):
        raise PermanentError(
            f"{spec.label} extracted but its .k2d files are missing -- "
            "the tarball layout may have changed upstream"
        )

    ctx.progress(phase="done", pct=1.0, message=f"{spec.label} ready")
    log.info("kraken_db_download_finished", job_id=ctx.job_id, db_key=key)
    return {"db_key": key, "path": str(settings.kraken_dbs_dir / key)}
