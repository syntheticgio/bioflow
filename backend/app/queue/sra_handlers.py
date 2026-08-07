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
from app.storage import compress as compress_mod
from app.storage import detect as detect_mod

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

# Peak disk now briefly holds three things at once rather than two: the
# archive, fasterq-dump's plain FASTQ, and the bgzip'd copy being written
# beside it before the plain file is removed -- see docs/superpowers/specs/
# 2026-08-05-object-compression-design.md. At bgzip's measured ~6.3x ratio the
# compressed copy adds roughly EXTRACTION_FACTOR's own headroom back, so
# EXTRACTION_FACTOR is left as-is rather than inflated for a peak that lasts
# only as long as one file's compression.


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

    ctx.check_cancel()
    content_hashes = _compress_staged(ctx, fastq_files, accession)

    staged = _describe(content_hashes)

    ctx.progress(phase="done", pct=1.0, message=f"downloaded {accession}")
    log.info(
        "sra_download_finished",
        job_id=ctx.job_id,
        accession=accession,
        files=len(staged),
        bytes=sum(p.stat().st_size for p in content_hashes),
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

    def on_line(line: str) -> None:
        # prefetch's own `\r`-redrawn bar reports bytes downloaded rather than
        # a clean percentage, and its exact wording has moved across toolkit
        # versions -- not worth guessing a regex for. What matters is that the
        # bar keeps moving during a multi-minute fetch instead of sitting at
        # the phase's opening pct=0.0 the whole time it runs.
        stripped = line.strip()
        if stripped:
            ctx.progress(phase="prefetch", message=stripped)

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
        on_line=on_line,
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


def _compress_staged(
    ctx: JobContext, fastq_files: list[Path], accession: str
) -> dict[Path, str | None]:
    """Bgzip each staged FASTQ before it is described and handed to the applier.

    Run here rather than left to `ingest_local_file`'s own compression (which
    still applies to every other ingest path) because this is the one place
    with a `JobContext`: the applier that actually calls `ingest_local_file`
    for an SRA download runs later, as a plain async function with no job to
    report progress or check cancellation against. See docs/superpowers/specs/
    2026-08-05-object-compression-design.md.

    fasterq-dump's own output is always plain FASTQ (`should_compress` will
    say yes for every file here), but the check still runs rather than being
    assumed, since a future fasterq-dump flag or a differently-shaped staged
    file should not silently double-compress.

    Returns the new (compressed) paths mapped to the plaintext hash each was
    compressed from -- None for a file left uncompressed. Carried through
    `_describe` into the staged dict so `ingest_local_file` can dedup by
    content even though it never sees the plaintext itself.
    """
    total_bytes = sum(f.stat().st_size for f in fastq_files)
    done_bytes = 0
    content_hashes: dict[Path, str | None] = {}

    for i, path in enumerate(fastq_files):
        ctx.check_cancel()
        detection = detect_mod.detect(path, path.name)
        if not compress_mod.should_compress(detection.kind, detection.compression):
            content_hashes[path] = None
            done_bytes += path.stat().st_size
            continue

        ctx.progress(
            phase="compressing",
            pct=min(0.95 + (done_bytes / total_bytes) * 0.04, 0.99) if total_bytes else 0.95,
            message=f"compressing {path.name} ({i + 1}/{len(fastq_files)})",
        )
        result = compress_mod.compress_and_hash(
            path, dest_dir=path.parent, cancel_event=ctx.cancel_event
        )
        gz_path = path.with_name(path.name + ".gz")
        result.path.rename(gz_path)
        # `path` is the plain FASTQ that fasterq-dump wrote; `done_bytes`
        # tracks against the plaintext total computed above, not the
        # (smaller) compressed size, so progress still reaches 1.0 by the
        # last file regardless of each file's ratio.
        done_bytes += path.stat().st_size
        path.unlink()
        content_hashes[gz_path] = result.content_sha256

    log.info(
        "sra_download_compressed",
        job_id=ctx.job_id,
        accession=accession,
        plain_bytes=total_bytes,
        compressed_bytes=sum(f.stat().st_size for f in content_hashes),
    )
    return content_hashes


def _describe(content_hashes: dict[Path, str | None]) -> list[dict]:
    """Label each staged file with its mate role.

    Derived from fasterq-dump's own `_1`/`_2` suffixes rather than by
    re-detecting the pair from the filenames: the tool already said which is
    which, and `pairing.py`'s inference exists for files that arrived without
    that guarantee. Matched against the name with any trailing `.gz` from
    `_compress_staged` stripped first, so a compressed run pairs exactly like
    an uncompressed one always did.
    """
    from app.storage.detect import strip_compression_suffix

    fastq_files = list(content_hashes)
    bare_names = {f: strip_compression_suffix(f.name) for f in fastq_files}
    paired = any(name.endswith(("_1.fastq", "_2.fastq")) for name in bare_names.values())

    staged = []
    for path in fastq_files:
        bare = bare_names[path]
        mate = None
        if paired:
            if bare.endswith("_1.fastq"):
                mate = "R1"
            elif bare.endswith("_2.fastq"):
                mate = "R2"
            else:
                # A third file alongside a pair: reads whose mate was filtered
                # out upstream. Real data, but not part of the pair.
                mate = "unpaired"
        staged.append(
            {
                "path": str(path),
                "name": path.name,
                "mate": mate,
                "content_sha256": content_hashes[path],
            }
        )
    return staged


def _download_failure(code: int, log_path: Path, accession: str) -> Exception:
    """Classify a non-zero exit from the SRA toolkit.

    Kept as a named wrapper so the call site reads the same; the logic is
    shared with the assembly handler in `download_failures`.
    """
    return download_failures.classify_failure(
        code, log_path, accession, tool="fasterq-dump"
    )
