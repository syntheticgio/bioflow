"""Downloading sequencing runs from NCBI SRA.

Separate from `pipeline_handlers` because the failure model differs: these jobs
are hours of network transfer against a service that rate-limits, where a trim
is minutes of local CPU. What they share -- shelling out, capturing output,
dying with the job -- comes from `executor.run_subprocess`.

Imported by `handlers.py` for the `@handler` registration side effects.
"""

import re
import shutil
from pathlib import Path

from app.config import settings
from app.errors import PermanentError, RetryableError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.pipelines import tools
from app.queue import download_failures
from app.queue.executor import run_subprocess
from app.queue.pipeline_handlers import _prepare_workdir
from app.queue.registry import HandlerMode, JobContext, handler

log = get_logger(__name__)

# fasterq-dump --progress writes a carriage-return bar; with --verbose it also
# reports its join phases. Both shapes appear here:
#   "lookup :|-------------------------------------------------- 100.00%"
#   "join   :|------------------------                            48.00%"
_PROGRESS_RE = re.compile(r"^\s*(\w+)\s*:\|[^|]*?([\d.]+)%")

# Space is checked against the estimate before spending an hour on a transfer
# that cannot land. The archive is compressed and fasterq-dump writes plain
# FASTQ, so the extracted size is several times the archive's -- and prefetch
# holds the archive at the same time.
EXTRACTION_FACTOR = 4.0


@handler(
    "download_sra_run",
    mode=HandlerMode.SUBPROCESS,
    # USER_INTERACTIVE: someone clicked download and is watching for the file.
    # The work is waiting on NCBI rather than computing, so it does not belong
    # in the compute class competing with alignments for CPU.
    job_class=JobClass.USER_INTERACTIVE,
    resources=JobResources(cpu=1, mem_mb=512, io=IoClass.HEAVY),
    # Higher than the pipeline handlers' 2: unlike a fastp failure, a failed
    # download is usually the network rather than the input, and the third
    # attempt genuinely succeeds often enough to be worth it.
    max_attempts=3,
)
def download_sra_run(ctx: JobContext) -> dict:
    """Fetch one SRA run as FASTQ. The ingest happens in the applier.

    Synchronous: SUBPROCESS runs this off the event loop, so the body must not
    await and cannot touch the database. It stages files under tmp/ and returns
    a description of what it staged for `_apply_sra_download` to persist.

    Idempotent by construction. Each attempt gets a fresh scratch directory
    (removed on entry), so a retry after a partial transfer starts clean rather
    than inheriting a truncated FASTQ -- which matters here more than elsewhere
    because fasterq-dump is not resumable.
    """
    fasterq = tools.require(tools.fasterq_dump())

    accession = (ctx.payload.get("accession") or "").strip().upper()
    if not accession:
        raise PermanentError("download_sra_run requires an 'accession'")

    project_id = ctx.payload.get("project_id")
    if not project_id:
        raise PermanentError("download_sra_run requires a 'project_id'")

    work = _prepare_workdir(ctx, kind="sra_download")

    # Checked *before* the transfer, not after. Discovering the disk is full
    # once the files exist is too late -- the space is already spent, and the
    # partial output has to be reaped anyway.
    _check_disk_space(work, ctx.payload.get("bytes_estimate"), accession)

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # The toolkit reads its configuration from here. Created by check_home at
    # startup, but a worker that somehow started without it must not lose a
    # download to a missing directory.
    settings.ncbi_dir.mkdir(parents=True, exist_ok=True)
    env = {"NCBI_SETTINGS": str(settings.ncbi_settings_path)}

    ctx.progress(phase="prefetch", pct=0.0, message=f"fetching {accession}")
    _prefetch(ctx, accession, work, log_path, env)

    ctx.check_cancel()

    ctx.progress(phase="converting", pct=0.1, message="converting to FASTQ")
    _fasterq_dump(ctx, fasterq.path, accession, work, log_path, env)

    fastq_files = sorted(p for p in work.glob("*.fastq") if p.is_file())
    if not fastq_files:
        # A zero exit with no output. Better caught here than as an ingest of
        # nothing several steps later.
        raise RetryableError(
            f"fasterq-dump exited 0 but produced no FASTQ for {accession}"
        )

    staged = _describe(fastq_files)

    ctx.progress(phase="done", pct=1.0, message=f"downloaded {accession}")
    log.info(
        "sra_download_finished",
        job_id=ctx.job_id,
        accession=accession,
        files=len(staged),
        bytes=sum(f.stat().st_size for f in fastq_files),
    )

    # No cleanup: the applier consumes these paths, and ingest_local_file
    # renames each file out of the staging directory. reap_pipeline_scratch
    # handles whatever a crashed run leaves behind.
    return {
        "accession": accession,
        "staged": staged,
        "metadata": ctx.payload.get("metadata") or {},
        "platform": ctx.payload.get("platform") or "UNKNOWN",
        "project_id": project_id,
        "run_qc": ctx.payload.get("run_qc", True),
        "job_id": ctx.job_id,
        "staging_dir": str(work),
    }


def _check_disk_space(work: Path, estimate: int | None, accession: str) -> None:
    """Refuse a download that cannot fit before spending an hour on it.

    Silent when the resolver could not supply an estimate: a missing figure is
    not evidence of a problem, and refusing on it would block downloads NCBI
    simply has no size for.
    """
    if not estimate:
        return

    free = shutil.disk_usage(work).free
    # The archive plus its extraction, with headroom. `estimate` is the
    # compressed archive size, and prefetch keeps it while fasterq-dump writes
    # the much larger FASTQ beside it.
    needed = estimate * EXTRACTION_FACTOR

    if needed > free * 0.9:
        raise PermanentError(
            f"Not enough disk space for {accession}: needs roughly "
            f"{needed / 1e9:.1f} GB (archive {estimate / 1e9:.1f} GB plus "
            f"extraction), only {free / 1e9:.1f} GB free.",
            details={
                "accession": accession,
                "needed_bytes": int(needed),
                "free_bytes": free,
            },
        )


def _prefetch(
    ctx: JobContext, accession: str, work: Path, log_path: Path, env: dict
) -> None:
    """Fetch the run into the local cache.

    Always run, rather than run on demand after fasterq-dump fails: some NCBI
    configurations require it, it is a no-op when the run is already cached,
    and detecting the specific vdb error to retry behind would be more fragile
    than paying for a no-op.

    Its failure is not fatal. fasterq-dump can fetch on its own, so a prefetch
    that fails leaves the download to it rather than ending the job here.
    """
    prefetch_tool = tools.prefetch()
    if not prefetch_tool.available:
        log.info("sra_prefetch_unavailable", job_id=ctx.job_id)
        return

    code = run_subprocess(
        ctx,
        [
            prefetch_tool.path,
            "--output-directory",
            str(work),
            "--max-size",
            "u",  # no size cap; the disk pre-flight is the real limit
            accession,
        ],
        log_path=str(log_path),
        env=env,
    )
    if code != 0:
        log.warning("sra_prefetch_failed", job_id=ctx.job_id, accession=accession, code=code)


def _fasterq_dump(
    ctx: JobContext,
    fasterq_path: str,
    accession: str,
    work: Path,
    log_path: Path,
    env: dict,
) -> None:
    cmd = [
        fasterq_path,
        "--outdir",
        str(work),
        "--temp",
        str(work),
        # Writes <acc>_1.fastq and <acc>_2.fastq for a paired run, plus a bare
        # <acc>.fastq for unpaired singletons. Without it a paired run becomes
        # one interleaved file that the aligner cannot mate up.
        "--split-files",
        "--progress",
        "--threads",
        str(min(settings.pipeline_default_threads, 4)),
        accession,
    ]

    def on_line(line: str) -> None:
        match = _PROGRESS_RE.match(line)
        if not match:
            return
        phase, raw = match.group(1), match.group(2)
        try:
            fraction = float(raw) / 100.0
        except ValueError:
            return
        # Capped below 1.0: the ingest still has to happen, and a bar sitting
        # at 100% while the file is not yet in the project reads as stuck.
        ctx.progress(
            pct=min(0.1 + fraction * 0.85, 0.95),
            phase=phase,
            message=f"{phase} {raw}%",
        )

    log.info(
        "sra_download_started", job_id=ctx.job_id, accession=accession, cmd=" ".join(cmd)
    )

    # A long transfer with no output for minutes at a time would otherwise let
    # the lease expire and the reaper double-run the job.
    ctx.extend_lease(3600)

    code = run_subprocess(ctx, cmd, log_path=str(log_path), on_line=on_line, env=env)
    if code != 0:
        raise _download_failure(code, log_path, accession)


def _describe(fastq_files: list[Path]) -> list[dict]:
    """Label each staged file with its mate role.

    Derived from fasterq-dump's own `_1`/`_2` suffixes rather than by
    re-detecting the pair from the filenames: the tool already said which is
    which, and `pairing.py`'s inference exists for files that arrived without
    that guarantee.
    """
    paired = any(f.name.endswith(("_1.fastq", "_2.fastq")) for f in fastq_files)

    staged = []
    for path in fastq_files:
        mate = None
        if paired:
            if path.name.endswith("_1.fastq"):
                mate = "R1"
            elif path.name.endswith("_2.fastq"):
                mate = "R2"
            else:
                # A third file alongside a pair: reads whose mate was filtered
                # out upstream. Real data, but not part of the pair.
                mate = "unpaired"
        staged.append({"path": str(path), "name": path.name, "mate": mate})
    return staged


def _download_failure(code: int, log_path: Path, accession: str) -> Exception:
    """Classify a non-zero exit from the SRA toolkit.

    Kept as a named wrapper so the call site reads the same; the logic is
    shared with the assembly handler in `download_failures`.
    """
    return download_failures.classify_failure(
        code, log_path, accession, tool="fasterq-dump"
    )
