"""Chunked alignment: orchestrator and merge handlers.

The orchestrator dispatches one standard align_reads sub-job per bucket,
tracks completion, and fires the merge step when all succeed. The merge
handler combines per-bucket sorted BAMs with samtools merge + sort.
"""

import time
from pathlib import Path

from app.config import settings
from app.errors import PermanentError, RetryableError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources, JobState
from app.models.job import Job
from app.pipelines import align_runner, tools
from app.queue.executor import run_subprocess
from app.queue.handlers import HandlerMode, handler

log = get_logger(__name__)

# Sub-job polling interval in seconds
_POLL_SECONDS = 5


@handler(
    "align_reads_chunked",
    mode=HandlerMode.ASYNC,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=1, mem_mb=128, io=IoClass.NONE),
    max_attempts=1,
)
async def align_reads_chunked(ctx):
    """Enqueue per-bucket align_reads sub-jobs and track completion."""
    import asyncio

    from app.queue import queue

    bucket_specs = ctx.payload.get("bucket_specs") or []
    if not bucket_specs:
        raise PermanentError("align_reads_chunked requires 'bucket_specs'")

    owner = ctx.payload.get("owner")
    total = len(bucket_specs)

    # Enqueue one align_reads sub-job per bucket
    sub_job_ids: list[str] = []
    for spec in bucket_specs:
        sub_payload = dict(ctx.payload)
        sub_payload["reference_path"] = spec["fasta_path"]
        sub_payload["parent_job_id"] = ctx.job_id
        sub_payload["chunk_bucket_index"] = spec["index"]
        sub_payload["chunk_total_buckets"] = total
        del sub_payload["bucket_specs"]
        sid = await queue.enqueue(
            "align_reads",
            payload=sub_payload,
            owner=owner,
            parent_job_id=ctx.job_id,
        )
        sub_job_ids.append(sid)

    ctx.progress(phase="aligning", pct=0.0, message=f"Buckets 0/{total}")

    # Poll sub-jobs
    failed: str | None = None
    completed: int = 0
    deadline = time.time() + 86400  # 24h hard cap

    while time.time() < deadline:
        try:
            ctx.check_cancel()
        except Exception:
            # Cancel every sub-job so they don't orphan worker slots,
            # then re-raise.
            from app.queue.queue import request_cancel

            for cid in sub_job_ids:
                await request_cancel(cid)
            raise

        completed = 0
        for jid in sub_job_ids:
            try:
                job = await Job.get(jid)
            except Exception:
                log.warning("chunked_sub_job_lookup_failed", job_id=jid)
                continue
            if job and job.state == JobState.FAILED:
                failed = str(jid)
                break
            if job and job.state == JobState.CANCELLED:
                failed = str(jid)
                break
            if job and job.state == JobState.SUCCEEDED:
                completed += 1
        if failed:
            raise RetryableError(
                f"Chunked alignment bucket failed (job {failed}) — "
                "cannot produce a complete result"
            )
        if completed == total:
            break
        ctx.progress(
            phase="aligning",
            pct=completed / total,
            message=f"Buckets {completed}/{total}",
        )
        await asyncio.sleep(_POLL_SECONDS)
    else:
        # Loop exited without reaching completed == total — deadline expired.
        raise RetryableError(
            f"Chunked alignment timed out after 24h "
            f"({completed}/{total} buckets completed)"
        )

    ctx.progress(phase="merging", pct=1.0, message="Merging buckets")

    return {
        "bucket_count": total,
        "sub_job_ids": sub_job_ids,
        "reference_id": ctx.payload.get("reference_id"),
        "project_id": ctx.payload.get("project_id"),
        "reads_object_id": ctx.payload.get("reads_object_id"),
        "aligner": ctx.payload.get("aligner"),
        "params": ctx.payload.get("params"),
        "owner": owner,
        "output_name": ctx.payload.get("output_name", "merged.bam"),
    }


@handler(
    "merge_chunked_buckets",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=4, mem_mb=4096, io=IoClass.HEAVY),
    max_attempts=2,
)
def merge_chunked_buckets(ctx):
    """Merge per-bucket sorted BAMs with samtools merge and sort."""
    samtools = tools.require(tools.samtools())
    bucket_bams_str = ctx.payload.get("bucket_bam_paths") or []
    bucket_bams = [Path(p) for p in bucket_bams_str]

    output_name = ctx.payload.get("output_name", "merged.bam")
    work_dir = Path(ctx.payload.get("workdir", settings.tmp_dir))
    work_dir.mkdir(parents=True, exist_ok=True)

    unsorted = work_dir / output_name.replace(".bam", ".unsorted.bam")
    output_bam = work_dir / output_name
    log_dir = settings.logs_dir
    log_dir.mkdir(parents=True, exist_ok=True)

    # samtools merge
    merge_cmd = [
        samtools.path, "merge", "-c", "-p", str(unsorted),
    ] + [str(b) for b in bucket_bams]
    merge_log = log_dir / f"{ctx.job_id}-merge.log"

    merge_progress = align_runner.SamtoolsProgress()
    code = run_subprocess(ctx, merge_cmd, log_path=str(merge_log), parser=merge_progress)
    if code != 0:
        raise RetryableError(f"samtools merge exited {code} — see {merge_log}")

    # samtools sort
    threads = min(ctx.payload.get("threads", 4), 8)
    sort_memory_mb = ctx.payload.get("sort_memory_mb", 1024)
    sort_cmd = align_runner.build_sort_command(
        samtools_path=samtools.path,
        bam=unsorted,
        output=output_bam,
        threads=threads,
        sort_memory_mb=sort_memory_mb,
    )
    sort_log = log_dir / f"{ctx.job_id}-sort.log"

    sort_progress = align_runner.SamtoolsProgress()
    code = run_subprocess(ctx, sort_cmd, log_path=str(sort_log), parser=sort_progress)
    if code != 0:
        raise RetryableError(f"samtools sort exited {code} — see {sort_log}")

    # Clean up unsorted BAM
    unsorted.unlink(missing_ok=True)

    # flagstat
    flagstat_cmd = align_runner.build_flagstat_command(
        samtools_path=samtools.path, bam=output_bam,
    )
    flagstat_log = log_dir / f"{ctx.job_id}-flagstat.log"
    flagstat_out = _run_subprocess(ctx, flagstat_cmd, flagstat_log, capture_stdout=True)

    flagstat = align_runner.parse_flagstat(flagstat_out)

    return {
        "output_path": str(output_bam),
        "flagstat": flagstat,
        "project_id": ctx.payload.get("project_id"),
        "reference_id": ctx.payload.get("reference_id"),
        "reads_object_id": ctx.payload.get("reads_object_id"),
        "aligner": ctx.payload.get("aligner"),
        "params": ctx.payload.get("params"),
        "bucket_count": ctx.payload.get("bucket_count", 0),
        "output_name": output_name,
    }


def _run_subprocess(ctx, cmd, log_path, capture_stdout=False):
    """Run a subprocess with logging, returning exit code or stdout."""
    import shlex
    import subprocess as sp

    if capture_stdout:
        proc = sp.run(cmd, capture_output=True)
        with open(log_path, "w") as log_f:
            log_f.write(f"# {' '.join(shlex.quote(str(a)) for a in cmd)}\n\n")
            if proc.stdout:
                log_f.write(proc.stdout.decode(errors="replace"))
            if proc.stderr:
                log_f.write("\n# stderr:\n" + proc.stderr.decode(errors="replace"))
        return str(proc.stdout.decode(errors="replace"))
    else:
        with open(log_path, "w") as log_f:
            log_f.write(f"# {' '.join(shlex.quote(str(a)) for a in cmd)}\n\n")
            proc = sp.run(cmd, stdout=log_f, stderr=sp.STDOUT)
        return int(proc.returncode)
