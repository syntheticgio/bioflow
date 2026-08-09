"""Pipeline job handlers.

Separate from `handlers.py` because these shell out to external tools and carry
a different failure model: an exit code rather than an exception, output that
must be captured to disk, and a child process that has to die with the job.

Imported by `handlers.py` for the `@handler` registration side effects.
"""

import asyncio
import shutil
from datetime import UTC, datetime
from pathlib import Path

from app.config import settings
from app.errors import PermanentError, RetryableError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.pipelines import cutadapt_runner, fastp_runner, qc_stats, tools, trimmomatic_runner
from app.pipelines.align_runner import ReadChemistry
from app.queue.executor import run_subprocess
from app.queue.registry import HandlerMode, JobContext, handler
from app.storage.paths import blob_path

log = get_logger(__name__)

# Platforms whose reads NanoPlot describes and FastQC does not. Matched against
# the SRA `PLATFORM` element's tag name, which is what the resolver records --
# so these are NCBI's spellings, not free text. `qc_stats.LONG_READ_PLATFORMS`
# is the single source for this set; see its comment for why it used to be
# three independent copies.
LONG_READ_PLATFORMS = frozenset(qc_stats.LONG_READ_PLATFORMS)


@handler(
    "trim_reads",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=4, mem_mb=2048, io=IoClass.HEAVY),
    # Low on purpose. A fastp failure is almost always deterministic -- bad
    # input, a missing binary, a full disk -- and spending five attempts on a
    # multi-hour run delays the error without making it less likely.
    max_attempts=2,
)
def trim_reads(ctx: JobContext) -> dict:
    """Adapter-trim and quality-filter a FASTQ file or an R1/R2 pair.

    Runs off the event loop in a worker thread, so it cannot touch the
    database: it resolves its inputs from the payload and returns a plain dict
    for `results._apply_trim_reads` to persist. See queue/results.py.

    Dispatches on the payload's `tool` (default "fastp") to one of three
    private functions below, each of which owns its own tool's command
    construction, progress reporting, and report parsing -- mirroring how
    run_qc dispatches on `platform`.

    Idempotent by construction. Delivery is at-least-once, and a drain during
    shutdown requeues a running job, so a second attempt must converge rather
    than collide with the first. Each attempt gets its own scratch directory,
    which is removed on entry -- a partial run leaves nothing behind that a
    retry could mistake for its own output.
    """
    object_id = ctx.payload.get("object_id")
    if not object_id:
        raise PermanentError("trim_reads requires an 'object_id'")

    tool = (ctx.payload.get("tool") or "fastp").lower()
    dispatch = {
        "fastp": _run_fastp_trim,
        "cutadapt": _run_cutadapt_trim,
        "trimmomatic": _run_trimmomatic_trim,
    }
    run = dispatch.get(tool)
    if run is None:
        raise PermanentError(f"trim_reads has no code path for tool {tool!r}")

    return run(ctx, object_id)


# The real filenames the Debian `trimmomatic` package installs under
# settings.trimmomatic_adapters_dir -- confirmed against the Docker image
# during planning. adapter_file is user-controllable (it rides in through
# TrimmomaticParams.from_dict from a job payload), and build_command
# concatenates it unescaped into an ILLUMINACLIP:<dir>/<file>:2:30:10
# argument, so an unlisted value is rejected here rather than trusted --
# see _run_trimmomatic_trim's docstring for why.
_TRIMMOMATIC_ADAPTER_FILES = frozenset({
    "NexteraPE-PE.fa",
    "TruSeq2-PE.fa",
    "TruSeq2-SE.fa",
    "TruSeq3-PE-2.fa",
    "TruSeq3-PE.fa",
    "TruSeq3-SE.fa",
})


def _check_trimmomatic_adapter_file(adapter_file: str | None) -> None:
    """Reject any adapter_file that is not a real file this app ships.

    A pure, directly testable check, pulled out of _run_trimmomatic_trim so a
    unit test can exercise it without spinning up a job context. See
    _run_trimmomatic_trim's docstring for why this exists.
    """
    if adapter_file is not None and adapter_file not in _TRIMMOMATIC_ADAPTER_FILES:
        raise PermanentError(f"Unknown Trimmomatic adapter file: {adapter_file!r}")


def _resolve_trim_inputs(ctx: JobContext, work: Path) -> tuple[Path, Path | None, bool]:
    """Resolve and name-link R1 (and R2, if present) for any trim tool.

    Shared across all three tool paths: every one of them needs its input
    symlinked under its real filename for the same reason fastp does --
    gzip-vs-text sniffing from a managed blob's extensionless hash name.
    """
    r1_in = _resolve_input(ctx.payload, "r1")
    r2_in = _resolve_input(ctx.payload, "r2") if ctx.payload.get("r2_sha256") else None
    paired = r2_in is not None

    r1_in = _named_link(work, r1_in, ctx.payload.get("r1_name"))
    if paired:
        r2_in = _named_link(work, r2_in, ctx.payload.get("r2_name"))
    return r1_in, r2_in, paired


def _run_fastp_trim(ctx: JobContext, object_id: str) -> dict:
    """fastp trim -- the original trim_reads body, unchanged in behavior."""
    fastp = tools.require(tools.fastp())

    work = _prepare_workdir(ctx)
    r1_in, r2_in, paired = _resolve_trim_inputs(ctx, work)

    out_dir = work / "out"
    out_dir.mkdir(exist_ok=True)

    r1_name = fastp_runner.output_name(ctx.payload.get("r1_name") or r1_in.name)
    r1_out = out_dir / r1_name
    r2_out = None
    r2_name = None
    if paired:
        r2_name = fastp_runner.output_name(ctx.payload.get("r2_name") or r2_in.name)
        r2_out = out_dir / r2_name

    json_out = work / "fastp.json"
    html_out = work / "fastp.html"
    params = fastp_runner.TrimParams.from_dict(ctx.payload.get("params"))

    cmd = fastp_runner.build_command(
        fastp_path=fastp.path,
        r1_in=r1_in,
        r1_out=r1_out,
        r2_in=r2_in,
        r2_out=r2_out,
        json_out=json_out,
        html_out=html_out,
        params=params,
    )

    progress = fastp_runner.TrimProgress(expected_reads=ctx.payload.get("expected_reads"))
    ctx.progress(phase="starting", pct=0.0, message="starting fastp")

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    log.info("trim_started", job_id=ctx.job_id, tool="fastp", paired=paired, cmd=" ".join(cmd))

    code = run_subprocess(ctx, cmd, log_path=str(log_path), parser=progress)
    if code != 0:
        raise _failure(code, log_path, tool="fastp")

    for produced in filter(None, (r1_out, r2_out)):
        if not produced.exists() or produced.stat().st_size == 0:
            raise RetryableError(f"fastp exited 0 but produced no output at {produced.name}")

    ctx.progress(
        phase="reporting",
        pct=fastp_runner.MAX_MEASURED_PCT,
        message="reading report",
        phase_index=len(fastp_runner.PHASE_ORDER),
        phase_total=len(fastp_runner.PHASE_ORDER),
    )
    report = fastp_runner.parse_report(json_out)

    outputs = [{"tmp_path": str(r1_out), "name": r1_name, "mate": "R1" if paired else None}]
    if paired:
        outputs.append({"tmp_path": str(r2_out), "name": r2_name, "mate": "R2"})

    ctx.progress(phase="done", pct=1.0, message="trimming complete")
    log.info("trim_finished", job_id=ctx.job_id, tool="fastp", outputs=len(outputs))

    return {
        "object_id": object_id,
        "mate_object_id": ctx.payload.get("mate_object_id"),
        "project_id": ctx.payload.get("project_id"),
        "job_id": ctx.job_id,
        "outputs": outputs,
        "report": report,
        "params": params.as_dict(),
        "tool": "fastp",
        "tool_version": fastp.version,
        "html_path": str(html_out) if html_out.exists() else None,
        "workdir": str(work),
    }


def _run_cutadapt_trim(ctx: JobContext, object_id: str) -> dict:
    """cutadapt trim.

    No --verbose progress stream exists for cutadapt, so ctx.progress only
    reports "starting" and "done" -- the same as run_qc's NanoPlot path,
    which has the same limitation for the same reason (no line-oriented
    progress output to parse).
    """
    tool = tools.require(tools.cutadapt())

    work = _prepare_workdir(ctx)
    r1_in, r2_in, paired = _resolve_trim_inputs(ctx, work)

    out_dir = work / "out"
    out_dir.mkdir(exist_ok=True)

    r1_name = cutadapt_runner.output_name(ctx.payload.get("r1_name") or r1_in.name)
    r1_out = out_dir / r1_name
    r2_out = None
    r2_name = None
    if paired:
        r2_name = cutadapt_runner.output_name(ctx.payload.get("r2_name") or r2_in.name)
        r2_out = out_dir / r2_name

    json_out = work / "cutadapt.json"
    params = cutadapt_runner.CutadaptParams.from_dict(ctx.payload.get("params"))

    cmd = cutadapt_runner.build_command(
        cutadapt_path=tool.path,
        r1_in=r1_in,
        r1_out=r1_out,
        r2_in=r2_in,
        r2_out=r2_out,
        json_out=json_out,
        params=params,
    )

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    ctx.progress(phase="starting", pct=0.0, message="starting cutadapt")
    log.info("trim_started", job_id=ctx.job_id, tool="cutadapt", paired=paired, cmd=" ".join(cmd))

    code = run_subprocess(ctx, cmd, log_path=str(log_path))
    if code != 0:
        raise _failure(code, log_path, tool="cutadapt")

    for produced in filter(None, (r1_out, r2_out)):
        if not produced.exists() or produced.stat().st_size == 0:
            raise RetryableError(f"cutadapt exited 0 but produced no output at {produced.name}")

    ctx.progress(phase="reporting", pct=0.95, message="reading report")
    report = cutadapt_runner.parse_report(json_out)

    outputs = [{"tmp_path": str(r1_out), "name": r1_name, "mate": "R1" if paired else None}]
    if paired:
        outputs.append({"tmp_path": str(r2_out), "name": r2_name, "mate": "R2"})

    ctx.progress(phase="done", pct=1.0, message="trimming complete")
    log.info("trim_finished", job_id=ctx.job_id, tool="cutadapt", outputs=len(outputs))

    return {
        "object_id": object_id,
        "mate_object_id": ctx.payload.get("mate_object_id"),
        "project_id": ctx.payload.get("project_id"),
        "job_id": ctx.job_id,
        "outputs": outputs,
        "report": report,
        "params": params.as_dict(),
        "tool": "cutadapt",
        "tool_version": tool.version,
        "html_path": None,
        "workdir": str(work),
    }


def _run_trimmomatic_trim(ctx: JobContext, object_id: str) -> dict:
    """Trimmomatic trim.

    No JSON, no progress stream: the report comes from a `-summary <file>`
    Trimmomatic writes on exit, read by trimmomatic_runner.parse_summary.

    adapter_file is allowlisted against the real contents of
    settings.trimmomatic_adapters_dir before it ever reaches
    trimmomatic_runner.build_command, which concatenates it unescaped into an
    `ILLUMINACLIP:<dir>/<file>:2:30:10` argument -- an unvalidated value
    could path-traverse outside that directory or, since Trimmomatic steps
    are colon-delimited, inject an unrelated step (e.g. `CROP:1`) into the
    command. The params dataclass has no opinion on this; validating what a
    caller supplies is this handler's job, not the pure command builder's.
    """
    tool = tools.require(tools.trimmomatic())

    work = _prepare_workdir(ctx)
    r1_in, r2_in, paired = _resolve_trim_inputs(ctx, work)

    out_dir = work / "out"
    out_dir.mkdir(exist_ok=True)

    r1_name = trimmomatic_runner.output_name(ctx.payload.get("r1_name") or r1_in.name)
    r1_out = out_dir / r1_name
    r2_out = None
    r2_name = None
    unpaired_r1_out = None
    unpaired_r2_out = None
    if paired:
        r2_name = trimmomatic_runner.output_name(ctx.payload.get("r2_name") or r2_in.name)
        r2_out = out_dir / r2_name
        unpaired_r1_out = out_dir / f"unpaired.{r1_name}"
        unpaired_r2_out = out_dir / f"unpaired.{r2_name}"

    params = trimmomatic_runner.TrimmomaticParams.from_dict(ctx.payload.get("params"))
    _check_trimmomatic_adapter_file(params.adapter_file)
    summary_out = work / "summary.txt"

    cmd = trimmomatic_runner.build_command(
        trimmomatic_pe_path=settings.trimmomatic_pe_path,
        trimmomatic_se_path=settings.trimmomatic_se_path,
        adapters_dir=settings.trimmomatic_adapters_dir,
        r1_in=r1_in,
        r1_out=r1_out,
        summary_out=summary_out,
        r2_in=r2_in,
        r2_out=r2_out,
        unpaired_r1_out=unpaired_r1_out,
        unpaired_r2_out=unpaired_r2_out,
        params=params,
    )

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    ctx.progress(phase="starting", pct=0.0, message="starting Trimmomatic")
    log.info(
        "trim_started", job_id=ctx.job_id, tool="trimmomatic", paired=paired, cmd=" ".join(cmd)
    )

    code = run_subprocess(ctx, cmd, log_path=str(log_path))
    if code != 0:
        raise _failure(code, log_path, tool="trimmomatic")

    for produced in filter(None, (r1_out, r2_out)):
        if not produced.exists() or produced.stat().st_size == 0:
            raise RetryableError(f"Trimmomatic exited 0 but produced no output at {produced.name}")

    ctx.progress(phase="reporting", pct=0.95, message="reading summary")
    report = trimmomatic_runner.parse_summary(summary_out, paired=paired)

    outputs = [{"tmp_path": str(r1_out), "name": r1_name, "mate": "R1" if paired else None}]
    if paired:
        outputs.append({"tmp_path": str(r2_out), "name": r2_name, "mate": "R2"})

    ctx.progress(phase="done", pct=1.0, message="trimming complete")
    log.info("trim_finished", job_id=ctx.job_id, tool="trimmomatic", outputs=len(outputs))

    return {
        "object_id": object_id,
        "mate_object_id": ctx.payload.get("mate_object_id"),
        "project_id": ctx.payload.get("project_id"),
        "job_id": ctx.job_id,
        "outputs": outputs,
        "report": report,
        "params": params.as_dict(),
        "tool": "trimmomatic",
        "tool_version": tool.version,
        "html_path": None,
        "workdir": str(work),
    }


@handler(
    "run_qc",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    # HEAVY io, not light: QC streams the whole FASTQ exactly as a trim does,
    # and the governor's cap on concurrent heavy readers is what keeps a
    # handful of QC runs from being slower in aggregate than two. (The plan
    # said MEDIUM, which is not one of the three classes the model defines.)
    # 2048 rather than 1024: this is the ceiling for either path, and NanoPlot
    # loads read lengths and qualities into pandas before plotting, so a large
    # ONT run needs more headroom than fastp's streaming pass ever does. The
    # short-read path simply does not use what it reserves here.
    resources=JobResources(cpu=2, mem_mb=2048, io=IoClass.HEAVY),
    # Same reasoning as trim_reads: a QC failure is deterministic -- bad input
    # or a missing binary -- and retries only delay the error.
    max_attempts=2,
)
def run_qc(ctx: JobContext) -> dict:
    """Measure read quality, with the tool that suits the platform.

    Read-only, unlike trim: it derives no files, only a description of one.
    The structured numbers come back as facts for `_apply_run_qc` to persist;
    the reports land under qc_reports/<object_id>/ and are referenced by path.

    Two paths, chosen by the payload's `platform`:

    - **Long reads** (Nanopore, PacBio) get NanoPlot. FastQC's per-base model
      assumes every read is the same length, which is meaningless for a file
      whose reads run from 200 bp to 100 kb -- its per-position plots would be
      dominated by the handful of longest reads.
    - **Everything else** gets fastp + FastQC, the standard short-read pair.

    Absent or unrecognized platforms take the short-read path: it is the
    overwhelmingly common case, and fastp reports something useful for any
    FASTQ where NanoPlot on short reads is merely uninformative.

    Synchronous -- SUBPROCESS runs this off the event loop, so the body must
    not await. Database work happens in the applier.
    """
    object_id = ctx.payload.get("object_id")
    if not object_id:
        raise PermanentError("run_qc requires an 'object_id'")

    reads_in = _resolve_input(ctx.payload, "r1")
    work = _prepare_workdir(ctx, kind="qc")

    # Same reason as trim: fastp infers gzip from the filename, and a managed
    # blob is stored under its hash with no extension.
    name = ctx.payload.get("name")
    reads_in = _named_link(work, reads_in, name)

    report_dir = settings.qc_reports_dir / str(object_id)
    report_dir.mkdir(parents=True, exist_ok=True)

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    platform = (ctx.payload.get("platform") or "UNKNOWN").upper()
    if platform in LONG_READ_PLATFORMS:
        facts = _run_long_read_qc(
            ctx, reads_in, work, report_dir, log_path, object_id, platform
        )
    else:
        facts = _run_short_read_qc(ctx, reads_in, work, report_dir, log_path, object_id)

    facts["qc_status"] = "ok"
    facts["qc_platform"] = platform

    ctx.progress(phase="done", pct=1.0, message="QC complete")
    log.info(
        "qc_finished",
        job_id=ctx.job_id,
        object_id=object_id,
        platform=platform,
        tool=facts.get("qc_tool"),
    )

    return {
        "object_id": object_id,
        "project_id": ctx.payload.get("project_id"),
        "job_id": ctx.job_id,
        "facts": facts,
        "workdir": str(work),
    }


def _run_short_read_qc(
    ctx: JobContext,
    reads_in: Path,
    work: Path,
    report_dir: Path,
    log_path: Path,
    object_id: str,
) -> dict:
    """fastp (report-only) plus FastQC: the standard short-read pair.

    FastQC's failure is not the job's failure. fastp produces every number the
    UI charts, and FastQC is the optional extra that needs a JRE; letting a
    broken Java install fail a QC run would deny the user the facts that did
    parse.
    """
    fastp_tool = tools.require(tools.fastp())

    json_out = work / "fastp_qc.json"
    html_out = work / "fastp_qc.html"

    cmd = fastp_runner.build_qc_command(
        fastp_path=fastp_tool.path,
        r1_in=reads_in,
        json_out=json_out,
        html_out=html_out,
        threads=min(settings.pipeline_default_threads, 2),
    )

    progress = fastp_runner.TrimProgress(expected_reads=ctx.payload.get("expected_reads"))
    ctx.progress(phase="starting", pct=0.0, message="starting fastp")

    log.info("qc_started", job_id=ctx.job_id, object_id=object_id, cmd=" ".join(cmd))

    code = run_subprocess(ctx, cmd, log_path=str(log_path), parser=progress)
    if code != 0:
        raise _failure(code, log_path)

    facts = fastp_runner.parse_qc_facts(json_out)

    if html_out.exists():
        shutil.copyfile(html_out, report_dir / "fastp.html")
        # Relative to report_dir, which is already qc_reports_dir/<object_id> --
        # get_qc_report's `root` includes the object_id once, so a fact that
        # repeats it produces a path nothing was ever written to.
        facts["qc_fastp_report"] = "fastp.html"

    ctx.progress(phase="fastqc", pct=fastp_runner.MAX_MEASURED_PCT, message="running FastQC")
    fastqc_name = _run_fastqc(ctx, reads_in, report_dir, log_path)
    if fastqc_name:
        facts["qc_fastqc_report"] = fastqc_name

    # Present on every QC'd file, not only long ones, so a consumer never has
    # to treat its absence as "short read" by default.
    facts["qc_read_chemistry"] = ReadChemistry.SHORT.value

    return facts


def _run_long_read_qc(
    ctx: JobContext,
    reads_in: Path,
    work: Path,
    report_dir: Path,
    log_path: Path,
    object_id: str,
    platform: str,
) -> dict:
    """NanoPlot: read-length and quality distributions for Nanopore/PacBio.

    Unlike the short-read path there is no second tool to fall back on, so a
    NanoPlot failure fails the job. That is the right outcome: the alternative
    would be running FastQC on long reads and recording numbers whose per-base
    model does not apply, which is worse than no QC at all.
    """
    nanoplot = tools.require(tools.nanoplot())

    out_dir = report_dir / "nanoplot"
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        nanoplot.path,
        "--fastq",
        str(reads_in),
        "--outdir",
        str(out_dir),
        "-t",
        str(min(settings.pipeline_default_threads, 2)),
        # A machine-readable stats file beside the HTML. Without it the summary
        # numbers would have to be scraped back out of the report.
        "--tsv_stats",
        # N50 is the headline number for a long-read run, the way Q30 is for a
        # short-read one.
        "--N50",
    ]

    ctx.progress(phase="nanoplot", pct=0.1, message="running NanoPlot")
    log.info("qc_started", job_id=ctx.job_id, object_id=object_id, cmd=" ".join(cmd))

    # NanoPlot reads the whole file before plotting and says little meanwhile,
    # so a large ONT run can sit quiet long enough to worry the reaper.
    ctx.extend_lease(1800)

    code = run_subprocess(ctx, cmd, log_path=str(log_path))
    if code != 0:
        raise _failure(code, log_path, tool="NanoPlot")

    facts = _parse_nanoplot_stats(out_dir)
    facts["qc_tool"] = "NanoPlot"
    facts["qc_tool_version"] = nanoplot.version

    report = next(iter(sorted(out_dir.glob("NanoPlot-report.html"))), None)
    if report is not None:
        # Relative to report_dir (qc_reports_dir/<object_id>), matching
        # qc_fastp_report/qc_fastqc_report -- see the comment there.
        facts["qc_nanoplot_report"] = f"nanoplot/{report.name}"

    # This function is only reached when `platform in LONG_READ_PLATFORMS`
    # (checked by the caller), so the lookup below cannot legitimately miss.
    # Indexing rather than `.get(platform, platform)` is deliberate: the old
    # fall-through passed an unmapped SRA tag straight through as if it were
    # already in qc_stats' short vocabulary, and infer_chemistry silently
    # read that as UNKNOWN chemistry with a plausible-looking reason instead
    # of surfacing a real bug. A KeyError here means that invariant broke.
    chemistry, reason = qc_stats.infer_chemistry(
        platform=qc_stats.LONG_READ_PLATFORMS[platform],
        mean_read_length=facts.get("qc_mean_read_length"),
        mean_quality=facts.get("qc_mean_quality"),
    )
    facts["qc_read_chemistry"] = chemistry.value
    facts["qc_read_chemistry_reason"] = reason

    return facts


def _parse_nanoplot_stats(out_dir: Path) -> dict:
    """Summary numbers from NanoPlot's TSV.

    The file is two columns of `Metric<TAB>value` with human-facing metric
    names ("Mean read length"). Only the handful worth showing are mapped;
    an unreadable file costs the numbers but keeps the HTML report.
    """
    # NanoStats.txt, not .tsv: `--tsv_stats` selects the *format* of this file
    # rather than renaming it, so the extension stays .txt either way.
    stats = out_dir / "NanoStats.txt"
    if not stats.exists():
        log.warning("nanoplot_stats_missing", path=str(stats))
        return {}

    # NanoPlot already emits snake_case metric names, so these are its keys
    # verbatim. Only the scalar summary is kept: the file also carries ranked
    # lists ("longest_read_(with_Q):1" through :5) and per-threshold yield
    # rows, which belong in the HTML report rather than on every object.
    wanted = {
        "number_of_reads": "qc_total_reads",
        "number_of_bases": "qc_total_bases",
        "mean_read_length": "qc_mean_read_length",
        "median_read_length": "qc_median_read_length",
        "read_length_stdev": "qc_read_length_stdev",
        "n50": "qc_read_length_n50",
        "mean_qual": "qc_mean_quality",
        "median_qual": "qc_median_quality",
    }

    facts: dict = {}
    try:
        for line in stats.read_text().splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            target = wanted.get(parts[0].strip().lower())
            if target is None:
                continue
            try:
                facts[target] = float(parts[1].replace(",", ""))
            except ValueError:
                # A metric NanoPlot rendered as text rather than a number is
                # still worth showing.
                facts[target] = parts[1].strip()
    except OSError as e:
        log.warning("nanoplot_stats_unreadable", path=str(stats), error=str(e))
        return {}

    return facts


def _run_fastqc(
    ctx: JobContext, reads_in: Path, report_dir: Path, log_path: Path
) -> str | None:
    """Run FastQC into `report_dir`, returning the HTML's filename or None.

    Every failure here is swallowed to a warning. FastQC is the optional half
    of the QC pair -- it needs a JRE, and on a host missing one the fastp facts
    are still worth keeping.
    """
    fastqc_tool = tools.fastqc()
    if not fastqc_tool.available:
        log.info("qc_fastqc_unavailable", job_id=ctx.job_id, error=fastqc_tool.error)
        return None

    out_dir = report_dir / "fastqc"
    out_dir.mkdir(parents=True, exist_ok=True)

    # FastQC names its output after the input file, and the input here is the
    # `in_`-prefixed symlink that exists only to give fastp a filename it can
    # read the compression from. Linking the bare name beside it keeps that
    # implementation detail out of a report title the user reads.
    if reads_in.name.startswith("in_"):
        clean = reads_in.parent / reads_in.name[len("in_") :]
        if not clean.exists():
            try:
                clean.symlink_to(reads_in.resolve())
                reads_in = clean
            except OSError as e:
                # Cosmetic only -- a report named after the symlink is still a
                # correct report, so this must not fail the run.
                log.debug("qc_name_link_failed", error=str(e))

    code = run_subprocess(
        ctx,
        [fastqc_tool.path, "--outdir", str(out_dir), "--quiet", str(reads_in)],
        log_path=str(log_path),
    )
    if code != 0:
        log.warning("qc_fastqc_failed", job_id=ctx.job_id, code=code)
        return None

    html = next(iter(sorted(out_dir.glob("*_fastqc.html"))), None)
    if html is None:
        log.warning("qc_fastqc_no_report", job_id=ctx.job_id)
        return None
    return f"fastqc/{html.name}"


def _resolve_input(payload: dict, side: str) -> Path:
    """Locate an input read file from its digest or explicit path."""
    digest = payload.get(f"{side}_sha256")
    path_str = payload.get(f"{side}_path")

    if path_str:
        path = Path(path_str)
    elif digest:
        path = blob_path(digest)
    else:
        raise PermanentError(f"No input given: expected '{side}_sha256' or '{side}_path'")

    if not path.exists():
        # Permanent rather than retryable: a blob that is missing now will
        # still be missing in thirty seconds, and the file-verification job is
        # what notices and reports storage problems.
        raise PermanentError(f"Input reads not found: {path}")
    return path


def _named_link(work: Path, target: Path, name: str | None) -> Path:
    """A symlink to `target` under its user-facing name, inside the workdir.

    Falls back to the target itself when there is no name to use -- a
    register-in-place file already sits under its real name, so the link would
    add nothing.
    """
    if not name:
        return target

    safe = Path(name).name
    link = work / f"in_{safe}"
    link.unlink(missing_ok=True)
    try:
        link.symlink_to(target)
    except OSError as e:
        # Not fatal on its own, but the run will almost certainly fail on a
        # compressed input, so say why here rather than leaving a parse error
        # as the only clue.
        log.warning("input_link_failed", target=str(target), name=safe, error=str(e))
        return target
    return link


def _prepare_workdir(ctx: JobContext, kind: str = "trim") -> Path:
    """A clean scratch directory for this job, under tmp/.

    tmp/ shares a filesystem with objects/ (asserted at startup in
    storage/home.py), so placing a finished output into the store is an atomic
    rename rather than a copy of a file that may be tens of gigabytes.

    Removed and recreated on entry, so a retry after a crashed attempt starts
    from nothing rather than inheriting a half-written file.
    """
    work = settings.tmp_dir / kind / ctx.job_id
    if work.exists():
        log.info("workdir_reset", job_id=ctx.job_id, kind=kind, path=str(work))
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    return work


def _killed_by_signal(code: int) -> bool:
    """Whether an exit code means "the kernel killed this", either convention.

    `run_subprocess` hands back `subprocess.Popen.returncode`, and Python
    reports a signal death as the negative signal number -- SIGKILL is -9, not
    137. 137 is the shell's `128 + signal` form, which this process never
    produces on its own but which a tool invoked through a shell wrapper still
    can, so both are accepted.

    Only SIGKILL (9) and SIGTERM (15) count. Widening this to "any negative
    code" would claim SIGHUP as an OOM kill, and treating every code above 128
    as a signal would misread 255, which plenty of tools use as a plain
    "something went wrong".
    """
    return code in (-9, -15, 137, 143)


def _failure(code: int, log_path: Path, tool: str = "fastp") -> Exception:
    """Classify a non-zero exit from an external tool.

    The tail of the log goes into the message because the job record is where
    the user looks first, and "fastp exited 1" on its own tells them nothing.
    """
    tail = _log_tail(log_path)
    detail = f"{tool} exited {code}"
    if tail:
        detail = f"{detail}: {tail}"

    if _killed_by_signal(code):
        # A kill on this stack means the OOM killer.
        #
        # Under a cgroup hard limit the ceiling does not move, so the job dies
        # identically on every attempt -- job_max_attempts turns one dead job
        # into five full-length dead ones. Terminal, and the message names the
        # cause, which is only possible because the ceiling is known.
        hard_mem_mb = settings.bioflow_hard_mem_mb
        if hard_mem_mb:
            return PermanentError(
                f"{detail} (killed at the {hard_mem_mb} MB hard limit -- "
                f"this job needs more memory than the limit allows)"
            )
        # With no ceiling, this was the host OOM killer under transient
        # pressure: a quieter machine or fewer threads may well succeed.
        return RetryableError(f"{detail} (killed, most likely out of memory)")

    return PermanentError(detail)


def _log_tail(path: Path, *, lines: int = 5, max_chars: int = 600) -> str:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return ""
    tail = " / ".join(line.strip() for line in text.splitlines()[-lines:] if line.strip())
    return tail[:max_chars]


@handler(
    "reap_pipeline_scratch",
    mode=HandlerMode.ASYNC,
    job_class=JobClass.MAINTENANCE,
    resources=JobResources(cpu=1, mem_mb=64, io=IoClass.LIGHT),
)
async def reap_pipeline_scratch(ctx: JobContext) -> dict:
    """Remove trim working directories and expired job logs.

    Two failure modes make this necessary. A crashed or cancelled run leaves
    its outputs under tmp/trim/<job_id>, and those are whole FASTQ files --
    tens of gigabytes that nothing else would ever reclaim. Separately,
    `_apply_result` deliberately does not fail a job when the write-back
    throws, because the expensive work succeeded; that path also strands its
    scratch directory.

    Age is the only signal used. Checking whether the owning job is still
    running would be a race: a directory is created before the job is marked
    running, so a young one may belong to a job that has not started writing.
    The grace period sidesteps that entirely.
    """
    from datetime import timedelta

    from app.storage.home import check_home

    home = check_home()
    if not home.ok:
        return {"skipped": True, "reason": home.detail}

    scratch_grace_hours = float(ctx.payload.get("scratch_grace_hours", 6))
    log_retention_days = float(
        ctx.payload.get("log_retention_days", settings.pipeline_log_retention_days)
    )

    now = datetime.now(UTC)
    removed_dirs = 0
    # Every scratch root a pipeline handler writes into. An alignment workdir
    # holds a whole BAM plus samtools' sort spills, so one left behind by a
    # crashed run is tens of gigabytes that nothing else would reclaim.
    for kind in ("trim", "align", "qc"):
        removed_dirs += await _reap_dir(
            ctx,
            settings.tmp_dir / kind,
            cutoff=now - timedelta(hours=scratch_grace_hours),
            remove=lambda p: shutil.rmtree(p, ignore_errors=True),
            want_dirs=True,
        )
    removed_logs = await _reap_dir(
        ctx,
        settings.logs_dir,
        cutoff=now - timedelta(days=log_retention_days),
        remove=lambda p: p.unlink(missing_ok=True),
        want_dirs=False,
    )

    if removed_dirs or removed_logs:
        log.info("pipeline_scratch_reaped", dirs=removed_dirs, logs=removed_logs)
    return {"removed_scratch_dirs": removed_dirs, "removed_logs": removed_logs}


async def _reap_dir(ctx: JobContext, root: Path, *, cutoff, remove, want_dirs: bool) -> int:
    """Delete entries under `root` last modified before `cutoff`."""
    if not await asyncio.to_thread(root.exists):
        return 0

    removed = 0
    for entry in await asyncio.to_thread(lambda: sorted(root.iterdir())):
        ctx.check_cancel()
        try:
            if entry.is_dir() != want_dirs:
                continue
            mtime = datetime.fromtimestamp(entry.stat().st_mtime, UTC)
        except OSError:
            continue  # vanished underneath us; nothing to do
        if mtime > cutoff:
            continue
        await asyncio.to_thread(remove, entry)
        removed += 1
    return removed
