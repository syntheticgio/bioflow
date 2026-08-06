"""Assembly completeness (compleasm) and reference-based misassembly QC
(QUAST).

Both are separate from contiguity by design, not by omission: contiguity
(sequence_n50 and friends) is computed inline at ingest by
`storage/parsers._parse_fasta` and needs no job at all. This module exists
because completeness and misassembly detection each need an external tool
and can run for minutes to hours, so each is its own job -- one that a user
launches, that can fail without losing anything else, and whose progress is
not conflated with whatever else touched the object.

Imported by `handlers.py` for the `@handler` registration side effects.
"""

from pathlib import Path

from app.config import settings
from app.errors import PermanentError, RetryableError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.pipelines import assembly_qc_registry, completeness_runner, quast_runner, tools
from app.queue.executor import run_subprocess
from app.queue.lineage_handlers import lineage_present
from app.queue.pipeline_handlers import _failure, _named_link, _prepare_workdir, _resolve_input
from app.queue.registry import HandlerMode, JobContext, handler

log = get_logger(__name__)

# A bacterial genome against a small lineage is minutes; a vertebrate genome
# against a large one -- more markers, more sequence for miniprot to search --
# can run hours. Matches the reasoning `ASSEMBLY_LEASE_SECONDS` gives in
# assembly_handlers, at a smaller multiple since this does less work than the
# assembly it is scoring.
COMPLETENESS_LEASE_SECONDS = 3 * 3600


@handler(
    "assess_completeness",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=8, mem_mb=8192, io=IoClass.HEAVY),
    # One attempt, same reasoning assemble_reads gives: the input and the
    # tool are both deterministic, so a retry fails the same way twice, and a
    # genuine transient is better surfaced to the user than silently re-run.
    max_attempts=1,
)
def assess_completeness(ctx: JobContext) -> dict:
    """Score one assembly's completeness against a lineage-specific ortholog
    set.

    Requires the lineage dataset to already be present under
    `settings.lineages_dir` -- `download_lineage` is a separate job and a
    dependency of this one, not something fetched inline, so a completeness
    run never silently blocks on the network partway through.
    """
    tool = tools.require(tools.compleasm())

    lineage = (ctx.payload.get("lineage") or "").strip()
    if not lineage:
        raise PermanentError("assess_completeness requires a 'lineage'")
    odb = (ctx.payload.get("odb") or assembly_qc_registry.COMPLEASM_SPEC.odb).strip()

    library_path = settings.lineages_dir
    if not lineage_present(library_path, lineage, odb):
        raise PermanentError(
            f"The {lineage}_{odb} lineage dataset is not downloaded. "
            "Run the download first, then try again."
        )

    work = _prepare_workdir(ctx, "completeness")
    assembly = _resolve_input(ctx.payload, "assembly")
    # miniprot infers nothing from the filename the way Flye infers gzip
    # compression from it, but a named link keeps the workdir's contents
    # legible for anyone debugging a failed run from the log alone.
    assembly = _named_link(work, assembly, ctx.payload.get("assembly_name"))

    out_dir = work / "out"
    params = completeness_runner.CompletenessParams(
        threads=ctx.payload.get("threads") or 8,
        lineage=lineage,
        odb=odb,
    )

    cmd = completeness_runner.build_completeness_command(
        compleasm_path=tool.path,
        assembly=assembly,
        out_dir=out_dir,
        library_path=library_path,
        params=params,
    )

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    ctx.progress(phase="starting", pct=None, message=f"starting compleasm ({lineage})")
    ctx.extend_lease(COMPLETENESS_LEASE_SECONDS)

    log.info(
        "completeness_started",
        job_id=ctx.job_id,
        lineage=lineage,
        odb=odb,
        threads=params.threads,
    )

    code = run_subprocess(ctx, cmd, log_path=str(log_path))
    if code != 0:
        raise _failure(code, log_path, "compleasm")

    summary_path = out_dir / "summary.txt"
    if not summary_path.exists():
        # Retryable, the same reasoning assemble_reads gives for a missing
        # FASTA: an exit-0 run with no summary is most plausibly a disk that
        # filled during the final write. max_attempts=1 means this will not
        # actually retry -- the class is what tells the user whether
        # re-running is worth their time.
        raise RetryableError(
            "compleasm exited successfully but wrote no summary.txt"
        )

    facts = completeness_runner.parse_summary(
        summary_path.read_text(errors="replace")
    )
    if not facts:
        # Parsed to nothing rather than raising: parse_summary already logs
        # the reason, and a summary that failed to parse must not fail a job
        # that spent possibly hours running miniprot and hmmsearch.
        log.warning("completeness_summary_unparseable", job_id=ctx.job_id)
    facts["assembly_completeness_tool_version"] = tool.version

    ctx.progress(phase="done", pct=1.0, message="completeness scored")
    log.info(
        "completeness_finished",
        job_id=ctx.job_id,
        lineage=lineage,
        complete_pct=facts.get("assembly_completeness_complete_pct"),
    )

    return {
        "object_id": ctx.payload.get("object_id"),
        "job_id": ctx.job_id,
        "facts": facts,
        "workdir": str(work),
    }


# A 12 Mb yeast assembly runs in 3.0-4.4s (measured, 2026-08-05), so an hour
# is enormously generous -- but nothing vertebrate-sized has been measured
# yet, and a lease expiring mid-run is a worse failure than one set too long.
# A tenth of assess_completeness's three hours, matching QUAST's much
# smaller job relative to a lineage-scale compleasm/miniprot/hmmsearch run.
MISASSEMBLY_LEASE_SECONDS = 3600

# The label QUAST is always run with here, and the filename its input is
# always linked under. Both fixed, never taken from the object's own name --
# see the module-level security note below.
_ASSEMBLY_LABEL = "assembly"
_ASSEMBLY_LINK_NAME = "assembly.fasta"


@handler(
    "assess_misassemblies",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=4, mem_mb=8192, io=IoClass.HEAVY),
    # Deterministic tool, deterministic pair of inputs -- a retry fails the
    # same way twice, the same reasoning assess_completeness gives.
    max_attempts=1,
)
def assess_misassemblies(ctx: JobContext) -> dict:
    """Reference-based misassembly QC for one assembly, with QUAST.

    Read-only like completeness: no new object, only facts merged onto the
    assembly that was scored.

    **The assembly's input filename must never reach QUAST.** QUAST
    sanitizes contig names (`qutils.correct_name`,
    `re.sub(r'[^\\w\\._\\-]', '_', ...)`) but not the assembly label
    (`qutils.correct_asm_label`, strip and truncate only), and the label is
    otherwise taken straight from the input filename. Verified by exploiting
    it: an input named `ev<img src=x onerror=alert(7)>.fasta` puts that tag
    verbatim and unescaped into `report.html`. `assess_completeness` links
    its input under the caller's own object name
    (`ctx.payload.get("assembly_name")`) -- copying that pattern here would
    make this a stored XSS the day that report is served without `sandbox`
    (Phase 6). So unlike every other handler in this module, the assembly is
    linked under a **fixed** name and passed a **fixed** `-l` label,
    regardless of what the object is actually called. The reference's
    filename does not have this problem -- QUAST runs it through the
    sanitizing `correct_name`, confirmed against the same hostile-filename
    test -- so it keeps its normal `_named_link` treatment, the same as
    every other handler's reference input.
    """
    tool = tools.require(tools.quast())

    work = _prepare_workdir(ctx, "misassembly")

    assembly = _resolve_input(ctx.payload, "assembly")
    assembly = _named_link(work, assembly, _ASSEMBLY_LINK_NAME)

    reference = _resolve_input(ctx.payload, "reference")
    reference = _named_link(work, reference, ctx.payload.get("reference_name"))

    min_contig = int(ctx.payload.get("min_contig") or 500)
    threads = max(1, int(ctx.payload.get("threads") or 4))

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    out_dir = work / "out"
    cmd = quast_runner.build_quast_command(
        quast_path=tool.path,
        assembly=assembly,
        reference=reference,
        out_dir=out_dir,
        threads=threads,
        min_contig=min_contig,
        label=_ASSEMBLY_LABEL,
    )

    ctx.progress(phase="starting", pct=None, message="starting quast")
    ctx.extend_lease(MISASSEMBLY_LEASE_SECONDS)

    log.info(
        "misassembly_started",
        job_id=ctx.job_id,
        reference_object_id=ctx.payload.get("reference_object_id"),
        min_contig=min_contig,
        threads=threads,
    )

    code = run_subprocess(ctx, cmd, log_path=str(log_path))
    if code != 0:
        raise _failure(code, log_path, "quast")

    report_path = out_dir / "report.tsv"
    if not report_path.exists():
        # Retryable, the same reasoning assess_completeness gives for a
        # missing summary.txt: an exit-0 run with no report is most
        # plausibly a disk that filled during the final write.
        # max_attempts=1 means this will not actually retry -- the class is
        # what tells the user whether re-running is worth their time.
        raise RetryableError("quast.py exited successfully but wrote no report.tsv")

    facts = quast_runner.parse_report_tsv(report_path.read_text(errors="replace"))

    # Not label-suffixed, unlike the .gff and .mis_contigs.* files in the
    # same directory -- confirmed against a real run with -l assembly.
    misassemblies_report_path = out_dir / "contigs_reports" / "misassemblies_report.tsv"
    if misassemblies_report_path.exists():
        facts.update(
            quast_runner.parse_misassemblies_report(
                misassemblies_report_path.read_text(errors="replace")
            )
        )
    else:
        # Not fatal: report.tsv already carries the total misassembly count.
        # The relocation/translocation/inversion breakdown is missing, not
        # the whole result -- see parse_misassemblies_report's own posture
        # on a summary that fails to read.
        log.warning("misassembly_breakdown_missing", job_id=ctx.job_id)

    if not facts:
        log.warning("misassembly_report_unparseable", job_id=ctx.job_id)

    # Every number above is a claim about *this assembly relative to that
    # reference* -- a misassembly count against a different-species
    # reference measures real biology as error and reads as a defect in the
    # assembly. These are not optional: without them the fact set has no way
    # to say what it was actually measured against.
    facts["assembly_misassembly_tool"] = "quast"
    facts["assembly_misassembly_tool_version"] = tool.version
    facts["assembly_misassembly_min_contig"] = min_contig
    facts["assembly_reference_id"] = ctx.payload.get("reference_object_id")
    facts["assembly_reference_name"] = ctx.payload.get("reference_name")

    ctx.progress(phase="done", pct=1.0, message="misassembly QC complete")
    log.info(
        "misassembly_finished",
        job_id=ctx.job_id,
        total=facts.get("assembly_misassembly_total"),
        genome_fraction=facts.get("assembly_reference_genome_fraction_pct"),
    )

    return {
        "object_id": ctx.payload.get("object_id"),
        "job_id": ctx.job_id,
        "facts": facts,
        "workdir": str(work),
    }
