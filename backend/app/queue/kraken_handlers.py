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
from app.queue.executor import run_subprocess
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


# ── Read classification (Kraken2 + Bracken) ──────────────────────────

_CLASSIFY_LEASE_SECONDS = 2 * 3600


def build_classification_facts(
    *,
    kraken_rows: list[dict],
    bracken_rows: list[dict],
    metadata_organism: str | None,
    db_key: str,
    bracken_note: str | None,
) -> dict:
    """The facts payload for one classification run (spec K2-H2).

    `taxonomy` always; `taxonomy_mismatch` only when the check fires --
    its absence is itself the "metadata agrees or is absent" claim.
    """
    from app.pipelines import kraken_runner

    taxonomy = kraken_runner.top_taxa(kraken_rows, bracken_rows)
    taxonomy["db_key"] = db_key
    if bracken_note:
        taxonomy["bracken_skipped"] = bracken_note

    facts: dict = {"taxonomy": taxonomy}
    mismatch = kraken_runner.organism_mismatch(metadata_organism, kraken_rows)
    if mismatch is not None:
        facts["taxonomy_mismatch"] = mismatch
    return facts


@handler(
    "classify_reads",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    # mem_mb here is the floor; launch_classify_reads overrides per
    # database from the registry (spec K2-C3).
    resources=JobResources(cpu=4, mem_mb=9216, io=IoClass.HEAVY),
    max_attempts=1,
)
def classify_reads(ctx: JobContext) -> dict:
    """Classify one read set against a Kraken2 database, refine with Bracken.

    Kraken2 failure fails the run; Bracken failure or an unusable
    distribution is recorded in the facts and the run succeeds with
    Kraken2-only results (spec K2-H1).  Reports are copied to
    `qc_reports/<object_id>/kraken2/` -- the same shelf the QUAST and fastp
    reports use -- non-fatally.
    """
    from app.pipelines import kraken_runner, tools
    from app.pipelines.kraken_db_registry import KRAKEN_DBS, db_present
    from app.queue.pipeline_handlers import _failure, _prepare_workdir, _resolve_input

    kraken_tool = tools.require(tools.kraken2())

    db_key = (ctx.payload.get("db_key") or "").strip()
    if db_key not in KRAKEN_DBS or not db_present(db_key):
        raise PermanentError(
            f"kraken database {db_key!r} is not on disk -- the download "
            "dependency should have run first"
        )
    db_dir = settings.kraken_dbs_dir / db_key

    work = _prepare_workdir(ctx, "classify")
    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    reads = _resolve_input(ctx.payload, "reads")
    mate = None
    if ctx.payload.get("mate_sha256") or ctx.payload.get("mate_path"):
        mate = _resolve_input(ctx.payload, "mate")

    report = work / "kraken2_report.txt"
    bracken_out = work / "bracken_species.tsv"

    ctx.progress(phase="classifying", pct=None, message="running Kraken2")
    ctx.extend_lease(_CLASSIFY_LEASE_SECONDS)

    cmd = kraken_runner.build_kraken2_command(
        kraken2_path=kraken_tool.path,
        db_dir=db_dir,
        reads=reads,
        mate=mate,
        report=report,
        output=Path("/dev/null"),
        threads=max(1, int(ctx.payload.get("threads") or 4)),
        gzipped=reads.suffix == ".gz",
    )
    code = run_subprocess(ctx, cmd, log_path=str(log_path))
    if code != 0:
        raise _failure(code, log_path, "kraken2")

    kraken_rows = kraken_runner.parse_kraken_report(
        report.read_text() if report.exists() else ""
    )

    # -- Bracken: non-fatal refinement --------------------------------
    bracken_rows: list[dict] = []
    bracken_note: str | None = None
    bracken_tool = tools.bracken()
    if not bracken_tool.available:
        bracken_note = "bracken is not installed"
    else:
        ctx.progress(phase="abundance", pct=None, message="running Bracken")
        read_len = _nearest_bracken_read_len(ctx.payload.get("mean_read_length"))
        bcmd = kraken_runner.build_bracken_command(
            bracken_path=bracken_tool.path,
            db_dir=db_dir,
            report=report,
            output=bracken_out,
            read_len=read_len,
        )
        bcode = run_subprocess(ctx, bcmd, log_path=str(log_path))
        if bcode != 0:
            bracken_note = f"bracken exited {bcode}"
            log.warning("bracken_failed", job_id=ctx.job_id, code=bcode)
        else:
            bracken_rows = kraken_runner.parse_bracken_output(
                bracken_out.read_text() if bracken_out.exists() else ""
            )

    facts = build_classification_facts(
        kraken_rows=kraken_rows,
        bracken_rows=bracken_rows,
        metadata_organism=ctx.payload.get("organism"),
        db_key=db_key,
        bracken_note=bracken_note,
    )

    _copy_kraken_reports(ctx, report, bracken_out)

    ctx.progress(phase="done", pct=1.0, message="classification complete")
    log.info(
        "classification_finished",
        job_id=ctx.job_id,
        taxa=len(facts["taxonomy"]["taxa"]),
        mismatch="taxonomy_mismatch" in facts,
    )
    return {
        "object_id": ctx.payload.get("object_id"),
        "job_id": ctx.job_id,
        "facts": facts,
    }


def _nearest_bracken_read_len(mean: object) -> int:
    """Bracken only accepts lengths its distributions were built for:
    50..300 in steps of 50 on the pre-built databases.  Default 100
    (spec K2-R3)."""
    try:
        value = float(mean)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 100
    return min((50, 100, 150, 200, 250, 300), key=lambda s: abs(s - value))


def _copy_kraken_reports(ctx: JobContext, report: Path, bracken_out: Path) -> None:
    """Copy the raw reports where the QC-report endpoint serves them.

    Non-fatal, the `_copy_report` posture in assembly_qc_handlers: a run
    that produced real facts must not fail over an artifact copy.
    """
    object_id = ctx.payload.get("object_id")
    if not object_id:
        return
    dest = settings.qc_reports_dir / str(object_id) / "kraken2"
    try:
        dest.mkdir(parents=True, exist_ok=True)
        if report.exists():
            shutil.copyfile(report, dest / "kraken2_report.txt")
        if bracken_out.exists():
            shutil.copyfile(bracken_out, dest / "bracken_species.tsv")
    except OSError:
        log.warning("kraken_report_copy_failed", job_id=ctx.job_id)
