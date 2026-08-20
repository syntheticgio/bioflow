"""Generating a MultiQC aggregate report for a project.

Unlike every other QC job here, this one is scoped to a **project** rather
than an object: it summarises QC that other jobs already produced across
many objects and belongs to none of them. That shows up in three places --
the payload carries `project_id` instead of `object_id`, the output lands
under `settings.multiqc_reports_dir / <project_id>`, and there is no
applier, since a successful run changes no object's facts.

MultiQC parses raw tool output, not the HTML this application shows the
user, so this handler stages the files each QC job retained (see
`pipeline_handlers._retain_multiqc_input`) into one directory and runs
MultiQC over that. Staging rather than pointing MultiQC at
`qc_reports_dir` directly is what keeps sample names readable: MultiQC
derives a sample name from the file's own path, and the report directories
are named by object id, so scanning them in place would produce a report
whose rows are 24-character hex strings.

Objects QC'd before retention shipped have nothing to contribute. That is
an ordinary outcome, not a failure -- the card gates on the same condition
this handler re-checks, and a project of older objects gets a clear
"nothing to summarise" rather than an empty report.

Imported by `handlers.py` for the `@handler` registration side effects.
"""

import shutil
from pathlib import Path

from beanie import PydanticObjectId

from app.config import settings
from app.errors import PermanentError
from app.logging import get_logger
from app.models import JobClass
from app.models.object import DataObject
from app.pipelines import multiqc_runner, tools
from app.queue.executor import run_subprocess
from app.queue.registry import HandlerMode, JobContext, handler

log = get_logger(__name__)

# The retained files a project's objects may carry, as (fact key, relative
# path under the object's report dir). Keyed by fact rather than discovered
# by globbing so that a file left behind by something else -- a stale copy,
# a future tool's output -- cannot silently widen what a report covers.
#
# FastQC has no fact: `_run_fastqc` writes its zip into the report
# directory as a side effect of running at all, predating any retention
# work, so presence on disk is the only signal. Hence the glob below.
RETAINED_FACT_FILES: tuple[tuple[str, str], ...] = (
    ("qc_fastp_data", "fastp/fastp.json"),
    # QUAST and samtools stats joined in #702 -- the reads-side-only v1
    # scope from #624 deferred them because samtools stats needed a new
    # tool invocation, not just a copy of something already computed.
    ("assembly_misassembly_data", "quast/report.tsv"),
    ("bam_stats_data", "samtools/stats.txt"),
)

# FastQC writes `<readname>_fastqc.zip`; the name varies per object, so
# this is matched rather than named.
FASTQC_GLOB = "fastqc/*_fastqc.zip"

# Below this, a report is not an aggregate. One sample with two tools is a
# single-sample report the per-object QC tab already shows better.
MIN_CONTRIBUTING_OBJECTS = 2


def _object_label(obj) -> str:
    """A filesystem-safe, human-readable stem for one object's staged files.

    MultiQC takes its sample names from the paths it scans, so this string
    is what a user reads in the report's leftmost column. The object's name
    is used when it has one, with the id as a suffix to keep two identically
    named files apart, and as the whole label when it does not.

    Sanitising is not cosmetic: the label becomes a directory name, so a
    name carrying a path separator would otherwise write outside the
    staging directory.
    """
    raw = (obj.name or "").strip()
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in raw).strip("._")
    return f"{safe}_{obj.id}" if safe else str(obj.id)


def _copy_into(src: Path, dest: Path) -> bool:
    """Copy one staged file, reporting whether it landed.

    Best-effort per file: one unreadable input should cost that sample's
    row, not the whole report.
    """
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
    except OSError as e:
        log.warning("multiqc_stage_copy_failed", src=str(src), error=str(e))
        return False
    return True


def stage_multiqc_inputs(objects: list, stage_dir: Path) -> int:
    """Copy every object's retained QC output into `stage_dir`.

    Returns the number of objects that contributed at least one file.
    Copies rather than symlinks: MultiQC reads them either way, but a copy
    cannot be invalidated by a concurrent job rewriting the object's report
    directory mid-scan.

    Each object's files land in their own subdirectory named by
    `_object_label`, which is what makes the report's sample column
    readable -- and what stops two objects' identically-named `fastp.json`
    from overwriting each other.
    """
    contributing = 0

    for obj in objects:
        report_dir = settings.qc_reports_dir / str(obj.id)
        if not report_dir.is_dir():
            continue

        dest_root = stage_dir / _object_label(obj)
        staged_any = False

        for fact_key, rel in RETAINED_FACT_FILES:
            if not (obj.facts or {}).get(fact_key):
                continue
            src = report_dir / rel
            if not src.is_file():
                # The fact says it was retained and the file is gone -- an
                # offloaded object, or a report dir cleaned up by hand.
                # Costs that sample's row, not the whole report.
                log.warning(
                    "multiqc_retained_file_missing",
                    object_id=str(obj.id),
                    path=str(src),
                )
                continue
            staged_any |= _copy_into(src, dest_root / Path(rel).name)

        for src in sorted(report_dir.glob(FASTQC_GLOB)):
            staged_any |= _copy_into(src, dest_root / src.name)

        if staged_any:
            contributing += 1

    return contributing


def object_contributes(obj) -> bool:
    """Whether one object carries QC output MultiQC could parse.

    The card's gate and the handler's staging must agree on this, or a card
    offers a report the job then refuses to build. Sharing one predicate is
    what keeps them from drifting -- the alternative is the same rule
    written twice, in two modules, with only a user's confusion to report
    the disagreement.

    Checks disk rather than facts alone: a fact naming a file that has since
    been offloaded or cleaned up would otherwise count toward a report that
    cannot include it.
    """
    report_dir = settings.qc_reports_dir / str(obj.id)
    if not report_dir.is_dir():
        return False

    for fact_key, rel in RETAINED_FACT_FILES:
        if (obj.facts or {}).get(fact_key) and (report_dir / rel).is_file():
            return True

    return any(report_dir.glob(FASTQC_GLOB))


def count_summarizable(objects: list) -> int:
    """How many of `objects` carry QC output worth aggregating.

    Read by the Actions-tab card to decide whether to offer a report. The
    handler re-checks the same condition while staging rather than trusting
    this count, since the two run at different times.
    """
    return sum(1 for obj in objects if object_contributes(obj))


def report_generated_at(project_id) -> float | None:
    """When this project's MultiQC report was written, as a Unix timestamp.

    Read from the file's own mtime rather than stored alongside the job.
    The report *is* the artifact -- if it is deleted or restored by hand,
    the mtime moves with it, while a database column would go on describing
    a file that is no longer there.
    """
    report = multiqc_runner.report_path(
        settings.multiqc_reports_dir / str(project_id)
    )
    try:
        return report.stat().st_mtime
    except OSError:
        return None


def newest_qc_output_at(objects: list) -> float | None:
    """The most recent mtime across every object's retained QC output.

    Compared against `report_generated_at` to answer "has QC run since this
    report was generated". Deliberately a single newest-timestamp comparison
    rather than a record of which runs a report covered: the honest question
    the UI asks is whether the report is behind, and answering it this way
    needs no per-report manifest to keep in sync with the artifact.

    The consequence worth knowing: re-running QC on a file that was already
    included makes the report stale, which is correct, but so does re-running
    it with an identical result. Staleness here means "the inputs moved",
    not "the output would differ".
    """
    newest: float | None = None

    for obj in objects:
        report_dir = settings.qc_reports_dir / str(obj.id)
        if not report_dir.is_dir():
            continue

        candidates = [report_dir / rel for _, rel in RETAINED_FACT_FILES]
        candidates += list(report_dir.glob(FASTQC_GLOB))

        for path in candidates:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if newest is None or mtime > newest:
                newest = mtime

    return newest


@handler("multiqc_report", mode=HandlerMode.ASYNC, job_class=JobClass.USER_BACKGROUND)
async def generate_multiqc_report(ctx: JobContext) -> dict:
    """Build one project's aggregate QC report.

    Idempotent: the report is regenerated from whatever is on disk now and
    overwrites the previous one in place. There is no version history, so a
    duplicate delivery costs a second run rather than a second artifact.
    """
    project_id = ctx.payload.get("project_id")
    owner = ctx.payload.get("owner")
    if not project_id:
        raise PermanentError("multiqc_report requires a 'project_id'")
    if not owner:
        raise PermanentError("multiqc_report requires an 'owner'")

    multiqc = tools.require(tools.multiqc())

    objects = await DataObject.find(
        {"project_id": PydanticObjectId(project_id), "owner": owner}
    ).to_list()

    ctx.progress(phase="staging", pct=0.1, message="collecting QC output")

    work = settings.tmp_dir / "multiqc" / ctx.job_id
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    stage_dir = work / "stage"
    stage_dir.mkdir(parents=True, exist_ok=True)

    try:
        contributing = stage_multiqc_inputs(objects, stage_dir)
        if contributing < MIN_CONTRIBUTING_OBJECTS:
            raise PermanentError(
                f"Need at least {MIN_CONTRIBUTING_OBJECTS} files with QC results "
                f"to summarize; this project has {contributing}. Run QC on more "
                "files, or re-run it on files QC'd before aggregate reporting "
                "was added."
            )

        out_dir = settings.multiqc_reports_dir / str(project_id)
        out_dir.mkdir(parents=True, exist_ok=True)

        log_path = settings.logs_dir / f"{ctx.job_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = multiqc_runner.build_multiqc_command(
            multiqc_path=multiqc.path,
            input_dir=stage_dir,
            out_dir=out_dir,
        )

        ctx.progress(phase="running", pct=0.5, message="running MultiQC")
        log.info(
            "multiqc_started",
            job_id=ctx.job_id,
            project_id=str(project_id),
            objects=contributing,
            cmd=" ".join(cmd),
        )

        code = run_subprocess(ctx, cmd, log_path=str(log_path))
        if code != 0:
            raise PermanentError(
                f"MultiQC exited {code}; see the job log for details."
            )

        # Exit 0 is not evidence a report exists: MultiQC returns zero
        # having written nothing when it finds no parseable input.
        # Verified 2026-08-20.
        if not multiqc_runner.report_path(out_dir).is_file():
            raise PermanentError(
                "MultiQC ran but produced no report -- none of the staged QC "
                "output was in a format it recognises."
            )
    finally:
        # The staged copies are pure scratch; leaving them behind would
        # grow tmp/ by the size of every project's QC output per run.
        shutil.rmtree(work, ignore_errors=True)

    ctx.progress(phase="done", pct=1.0, message="report ready")
    log.info(
        "multiqc_finished",
        job_id=ctx.job_id,
        project_id=str(project_id),
        objects=contributing,
    )

    return {
        "project_id": str(project_id),
        "job_id": ctx.job_id,
        "objects_summarized": contributing,
        "report": multiqc_runner.REPORT_FILENAME,
    }
