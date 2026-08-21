"""CheckM2: download its database, and score a set of bins with it.

Two handlers, in the `kraken_handlers` shape and for the same reasons:

  * `download_checkm2_db` fetches reference data shared across every project.
    **No applier** -- a successful run leaves files under
    `settings.checkm2_db_dir / <key>` and nothing else changes state.
  * `score_bin_quality` runs `checkm2 predict` ONCE over a directory of bins
    (spec Q3). CheckM2's fixed cost is loading the DIAMOND database and it
    already takes a directory and emits one table, so one job per bin would
    pay that cost N times and put N queue entries behind one click.

The download streams with urllib for the reason `kraken_handlers` documents:
nothing else in this repo streams HTTP to disk, and a 1.7 GB tarball must not
be buffered in memory. Integrity is this handler's own job -- `checkm2
database --download` exists and is deliberately unused, because it resolves
its own URL at runtime, which is the moving target the pin exists to prevent
(spec Q1).

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
from app.pipelines.checkm2_db_registry import CHECKM2_DBS
from app.queue.executor import run_subprocess
from app.queue.registry import HandlerMode, JobContext, handler

log = get_logger(__name__)

_DOWNLOAD_LEASE_SECONDS = 3 * 3600  # 1.7 GB on a slow line takes a while
_PREDICT_LEASE_SECONDS = 4 * 3600


def verify_md5(tarball: Path, expected: str) -> None:
    """Raise PermanentError when the tarball does not match the registry.

    Permanent rather than retryable on its own: the *job* retries by
    re-downloading (max_attempts=3), but a mismatched file must never be
    extracted, and the message must say why.
    """
    h = hashlib.md5()
    with tarball.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    got = h.hexdigest()
    if got != expected:
        raise PermanentError(
            f"downloaded CheckM2 database failed md5 verification "
            f"(expected {expected}, got {got}) -- corrupt or altered download"
        )


def extract_and_promote(tarball: Path, final_dir: Path) -> None:
    """Extract into `<final>.partial`, rename to `final_dir` on success.

    The rename is the commit point: `db_present()` reads the final path, so an
    interrupted extraction is invisible to every consumer rather than leaving
    a half-populated directory that reads as present.
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
    "download_checkm2_db",
    mode=HandlerMode.SUBPROCESS,
    # USER_INTERACTIVE: someone pressed the score card and is waiting, the
    # same reasoning download_kraken_db gives.
    job_class=JobClass.USER_INTERACTIVE,
    resources=JobResources(cpu=1, mem_mb=512, io=IoClass.HEAVY),
    max_attempts=3,
)
def download_checkm2_db(ctx: JobContext) -> dict:
    """Fetch the CheckM2 database into the shared store.

    Idempotent: an already-present database returns immediately, so the dedup
    collapse in `launch_checkm2_db_download` plus this check makes a re-run a
    fast no-op rather than a duplicate 1.7 GB download.
    """
    from app.pipelines.checkm2_db_registry import db_present

    key = (ctx.payload.get("db_key") or "").strip()
    spec = CHECKM2_DBS.get(key)
    if spec is None:
        raise PermanentError(f"unknown CheckM2 database {key!r}")

    if db_present(key):
        return {"db_key": key, "already_present": True}

    settings.checkm2_db_dir.mkdir(parents=True, exist_ok=True)
    tarball = settings.checkm2_db_dir / f"{key}.tar.gz.partial"

    ctx.progress(phase="downloading", pct=None, message=f"downloading {spec.label}")
    ctx.extend_lease(_DOWNLOAD_LEASE_SECONDS)
    log.info("checkm2_db_download_started", job_id=ctx.job_id, db_key=key)

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
        extract_and_promote(tarball, settings.checkm2_db_dir / key)
    finally:
        tarball.unlink(missing_ok=True)

    if not db_present(key):
        raise PermanentError(
            f"{spec.label} extracted but its DIAMOND database file is "
            "missing -- the tarball layout may have changed upstream"
        )

    ctx.progress(phase="done", pct=1.0, message=f"{spec.label} ready")
    log.info("checkm2_db_download_finished", job_id=ctx.job_id, db_key=key)
    return {"db_key": key, "path": str(settings.checkm2_db_dir / key)}


@handler(
    "score_bin_quality",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    # mem_mb here is the floor; launch_bin_qc overrides it from the registry,
    # which knows the cost a priori rather than fitting it (spec Q1).
    resources=JobResources(cpu=4, mem_mb=16384, io=IoClass.HEAVY),
    # A deterministic failure (a bin CheckM2 cannot read, a missing database)
    # does not improve with retries.
    max_attempts=1,
)
def score_bin_quality(ctx: JobContext) -> dict:
    """Score every bin in one CheckM2 run (spec Q3).

    Returns one row per bin keyed by the bin's *object id*, so the applier
    never has to re-derive which object a table row belongs to. The join is
    made here, where the stem-to-object mapping that built the directory is
    still in hand.
    """
    from app.pipelines import checkm2_runner, tools
    from app.pipelines.checkm2_db_registry import CHECKM2_DBS, db_file, db_present
    from app.queue.align_handlers import _resolve_blob
    from app.queue.pipeline_handlers import _failure, _prepare_workdir

    checkm2_tool = tools.require(tools.checkm2())

    db_key = (ctx.payload.get("db_key") or "").strip()
    if db_key not in CHECKM2_DBS or not db_present(db_key):
        raise PermanentError(
            f"CheckM2 database {db_key!r} is not on disk -- the download "
            "dependency should have run first"
        )

    bins = ctx.payload.get("bins") or []
    if not bins:
        raise PermanentError("scoring requires at least one bin")

    work = _prepare_workdir(ctx, "checkm2")
    bins_dir = work / "bins"
    bins_dir.mkdir(parents=True, exist_ok=True)

    # Stem -> object id. CheckM2's table keys on the file stem, and that is
    # the only join back to the object, so the names are assigned here rather
    # than trusting whatever the bin objects happen to be called: two bins
    # from different runs can share a name, and a collision in this directory
    # would silently score one bin twice and the other not at all.
    stems: dict[str, str] = {}
    for entry in bins:
        object_id = str(entry.get("object_id") or "").strip()
        if not object_id:
            continue
        stem = f"bin_{object_id}"
        target = bins_dir / f"{stem}.fa"
        target.unlink(missing_ok=True)
        target.symlink_to(_resolve_blob(entry, "bin"))
        stems[stem] = object_id

    if not stems:
        raise PermanentError("none of the supplied bins could be resolved")

    out_dir = work / "out"
    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    ctx.progress(
        phase="scoring",
        pct=0.1,
        message=f"scoring {len(stems)} bins with CheckM2",
    )
    ctx.extend_lease(_PREDICT_LEASE_SECONDS)

    cmd = checkm2_runner.build_predict_command(
        checkm2_path=checkm2_tool.path,
        bins_dir=bins_dir,
        output_dir=out_dir,
        database_path=db_file(db_key),
        extension="fa",
        threads=max(1, int(ctx.payload.get("threads") or 4)),
        lowmem=bool(ctx.payload.get("lowmem")),
    )
    code = run_subprocess(ctx, cmd, log_path=str(log_path))
    if code != 0:
        raise _failure(code, log_path, "checkm2")

    report = out_dir / checkm2_runner.QUALITY_REPORT
    if not report.exists():
        raise PermanentError(
            "CheckM2 exited successfully but wrote no quality_report.tsv"
        )

    rows = checkm2_runner.parse_quality_report(report.read_text())
    scored: list[dict] = []
    for row in rows:
        object_id = stems.get(row.name)
        if object_id is None:
            # A row for a file this job did not put in the directory. Logged
            # rather than raised: it cannot be attributed to a bin, so there
            # is nothing to write it onto.
            log.warning("checkm2_unknown_row", name=row.name, job_id=ctx.job_id)
            continue
        scored.append(
            {
                "object_id": object_id,
                "facts": checkm2_runner.bin_quality_facts(row),
            }
        )

    _copy_report(ctx, report)

    ctx.progress(phase="done", pct=1.0, message=f"scored {len(scored)} bins")
    log.info(
        "bin_quality_scored",
        job_id=ctx.job_id,
        requested=len(stems),
        scored=len(scored),
    )
    return {
        "job_id": ctx.job_id,
        "assembly_id": ctx.payload.get("assembly_id"),
        "db_key": db_key,
        "scored": scored,
        "tool_version": checkm2_tool.version,
    }


def _copy_report(ctx: JobContext, report: Path) -> None:
    """Copy the raw table where the QC-report endpoint serves it.

    Non-fatal, the `_copy_kraken_reports` posture: a run that produced real
    facts must not fail over an artifact copy.
    """
    assembly_id = ctx.payload.get("assembly_id")
    if not assembly_id:
        return
    dest = settings.qc_reports_dir / str(assembly_id) / "checkm2"
    try:
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(report, dest / "quality_report.tsv")
    except OSError:
        log.warning("checkm2_report_copy_failed", job_id=ctx.job_id)
