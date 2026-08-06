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

import shutil
from pathlib import Path

from app.config import settings
from app.errors import PermanentError, RetryableError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.pipelines import (
    assembly_qc_registry,
    completeness_runner,
    craq_runner,
    quast_runner,
    tools,
)
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

    code = run_subprocess(
        ctx, cmd, log_path=str(log_path), parser=completeness_runner.CompletenessProgress()
    )
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

    report_fact = _copy_report(ctx, out_dir)
    if report_fact:
        facts["assembly_misassembly_report"] = report_fact

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


def _copy_report(ctx: JobContext, out_dir: Path) -> str | None:
    """Copy QUAST's HTML report tree into `qc_reports/<object_id>/quast/`,
    where `get_qc_report` serves it -- same storage shape `run_qc` uses for
    fastp's and FastQC's reports.

    Selective, not a directory copy: `out_dir` also holds `report.tex`,
    `report.pdf`, `transposed_report*`, `contigs_reports/*.stdout`, and
    several other artifacts this application has no reader for. Copying the
    whole tree would store bytes nothing ever serves.

    `report.html` and `icarus.html` are self-contained -- the only outbound
    `href` in a real report is a link to QUAST's own homepage -- but
    `icarus.html` still links to `icarus_viewers/*.html` as separate pages,
    so those come along too. The `.misassemblies.gff` is not part of the
    report page at all; it is copied alongside it because it carries
    per-breakpoint coordinates and types that make a bare count actionable,
    and `qc_reports/<object_id>/` is already the place this application
    keeps a run's human-readable artifacts.

    Returns None, logging a warning, rather than raising: a QUAST run that
    produced real facts must not fail the job over a report copy failing --
    the same posture the rest of this handler takes on parse failures.
    """
    object_id = ctx.payload.get("object_id")
    report_html = out_dir / "report.html"
    if not object_id or not report_html.exists():
        log.warning("misassembly_report_missing", job_id=ctx.job_id)
        return None

    report_dir = settings.qc_reports_dir / str(object_id) / "quast"
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(report_html, report_dir / "report.html")

        icarus_html = out_dir / "icarus.html"
        if icarus_html.exists():
            shutil.copyfile(icarus_html, report_dir / "icarus.html")

        icarus_viewers = out_dir / "icarus_viewers"
        if icarus_viewers.is_dir():
            shutil.copytree(
                icarus_viewers, report_dir / "icarus_viewers", dirs_exist_ok=True
            )

        gff = out_dir / "contigs_reports" / f"{_ASSEMBLY_LABEL}.misassemblies.gff"
        if gff.exists():
            shutil.copyfile(gff, report_dir / "misassemblies.gff")
    except OSError as e:
        # Cosmetic only, the same reasoning _run_fastqc's own copy failures
        # take: the facts are already computed and are the half of this
        # result a user cannot get any other way. A missing report page is
        # recoverable by re-running; lost facts from a job that already ran
        # for real are not.
        log.warning("misassembly_report_copy_failed", job_id=ctx.job_id, error=str(e))
        return None

    # Relative to report_dir, which is already qc_reports_dir/<object_id> --
    # get_qc_report's `root` includes the object_id once, so a fact that
    # repeats it names a path nothing was ever written to. Matches the
    # `qc_fastp_report`/`qc_fastqc_report` convention in pipeline_handlers.py.
    return "quast/report.html"


# CRAQ over pre-made BAMs skips the read-mapping step its own README calls
# the most time-consuming part, so this is far below assess_completeness's
# three hours. Matched to QUAST's hour until a real vertebrate-scale run is
# measured -- a lease expiring mid-run is a worse failure than one set long.
ASSEMBLY_ERROR_LEASE_SECONDS = 3600

# Fixed names, never the object's own. CRAQ is a Perl/shell pipeline that
# interpolates its inputs into `system()` calls (see bin/craq), so a
# filename carrying shell metacharacters is the analogue of the QUAST label
# XSS -- closed the same way, before it can exist.
_CRAQ_ASSEMBLY_LINK = "assembly.fasta"
_CRAQ_NGS_LINK = "ngs_sort.bam"
_CRAQ_SMS_LINK = "sms_sort.bam"


@handler(
    "assess_assembly_errors",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=4, mem_mb=8192, io=IoClass.HEAVY),
    max_attempts=1,
)
def assess_assembly_errors(ctx: JobContext) -> dict:
    """Reference-free assembly error detection for one assembly, with CRAQ.

    Read-only by default: no new object, only facts merged onto the assembly
    that was scored. `-b` chimera breaking is the one exception and is
    opt-in per run.

    **Input filenames never reach the command line.** CRAQ shells out
    through `system()` with its arguments interpolated, so unlike QUAST the
    risk is shell metacharacters rather than HTML. Every input is linked
    under a fixed name; the object's own name is recorded as a fact, not
    passed as an argument.

    **A BAM's index must travel with it.** CRAQ requires `sort.bam.bai`
    beside `sort.bam`; linking the BAM alone produces a failure deep in a
    samtools call rather than a clear error.
    """
    tool = tools.require(tools.craq())

    work = _prepare_workdir(ctx, "assembly_errors")

    assembly = _resolve_input(ctx.payload, "assembly")
    assembly = _named_link(work, assembly, _CRAQ_ASSEMBLY_LINK)

    ngs_bam = None
    if ctx.payload.get("ngs_bam_path") or ctx.payload.get("ngs_bam_sha256"):
        raw = _resolve_input(ctx.payload, "ngs_bam")
        ngs_bam = _named_link(work, raw, _CRAQ_NGS_LINK)
        _link_bam_index(raw, ngs_bam)

    sms_bam = None
    if ctx.payload.get("sms_bam_path") or ctx.payload.get("sms_bam_sha256"):
        raw = _resolve_input(ctx.payload, "sms_bam")
        sms_bam = _named_link(work, raw, _CRAQ_SMS_LINK)
        _link_bam_index(raw, sms_bam)

    if ngs_bam is None and sms_bam is None:
        raise PermanentError(
            "Assembly error detection needs at least one alignment of reads "
            "against this assembly."
        )

    threads = max(1, int(ctx.payload.get("threads") or 4))
    break_chimera = bool(ctx.payload.get("break_chimera"))

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    out_dir = work / "out"
    cmd = craq_runner.build_craq_command(
        craq_path=tool.path,
        assembly=assembly,
        ngs_bam=ngs_bam,
        sms_bam=sms_bam,
        out_dir=out_dir,
        threads=threads,
        break_chimera=break_chimera,
    )

    ctx.progress(phase="starting", pct=None, message="starting craq")
    ctx.extend_lease(ASSEMBLY_ERROR_LEASE_SECONDS)

    log.info(
        "assembly_errors_started",
        job_id=ctx.job_id,
        has_ngs=ngs_bam is not None,
        has_sms=sms_bam is not None,
        break_chimera=break_chimera,
        threads=threads,
    )

    code = run_subprocess(ctx, cmd, log_path=str(log_path))
    if code != 0:
        raise _failure(code, log_path, "craq")

    aqi_dir = out_dir / "runAQI_out"
    # `<genome basename>_final.Report` -- predictable only because the
    # assembly is linked under a fixed name above. `assembly` here is the
    # actual path CRAQ was invoked with (`_named_link` prefixes the
    # requested name with `in_`), so the report's basename must be derived
    # from it rather than from the bare `_CRAQ_ASSEMBLY_LINK` constant.
    report_path = aqi_dir / f"{assembly.name}_final.Report"
    if not report_path.exists():
        raise RetryableError("craq exited successfully but wrote no final report")

    has_ngs = ngs_bam is not None
    has_sms = sms_bam is not None

    facts = craq_runner.parse_final_report(
        report_path.read_text(errors="replace"), has_ngs=has_ngs, has_sms=has_sms
    )

    cre = craq_runner.count_bed_records(aqi_dir / "locER_out" / "out_final.CRE.bed")
    if cre is not None:
        facts["assembly_error_cre_count"] = cre
    crh = craq_runner.count_bed_records(aqi_dir / "locER_out" / "out_final.CRH.bed")
    if crh is not None:
        facts["assembly_error_crh_count"] = crh

    # Structural counts only when long reads were supplied -- the same rule
    # parse_final_report applies to S-AQI, for the same reason.
    if has_sms:
        cse = craq_runner.count_bed_records(aqi_dir / "strER_out" / "out_final.CSE.bed")
        if cse is not None:
            facts["assembly_error_cse_count"] = cse
        csh = craq_runner.count_bed_records(aqi_dir / "strER_out" / "out_final.CSH.bed")
        if csh is not None:
            facts["assembly_error_csh_count"] = csh

    if not facts:
        log.warning("assembly_errors_report_unparseable", job_id=ctx.job_id)

    # Which inputs produced these numbers is not optional metadata: a CRE
    # count from a long-read-only run is undercounted, and without these
    # flags nothing downstream can say so.
    facts["assembly_error_tool"] = "craq"
    facts["assembly_error_tool_version"] = tool.version
    facts["assembly_error_has_ngs"] = has_ngs
    facts["assembly_error_has_sms"] = has_sms

    ctx.progress(phase="done", pct=1.0, message="assembly error QC complete")
    log.info(
        "assembly_errors_finished",
        job_id=ctx.job_id,
        cre=facts.get("assembly_error_cre_count"),
        cse=facts.get("assembly_error_cse_count"),
        aqi=facts.get("assembly_error_aqi"),
    )

    return {
        "object_id": ctx.payload.get("object_id"),
        "job_id": ctx.job_id,
        "facts": facts,
        "workdir": str(work),
        "break_chimera": break_chimera,
        "corrected_fasta": str(aqi_dir / "out_correct.fa")
        if break_chimera and (aqi_dir / "out_correct.fa").exists()
        else None,
    }


def _link_bam_index(raw_bam: Path, linked_bam: Path) -> None:
    """Link a BAM's `.bai` beside its fixed-name link.

    CRAQ requires the index next to the BAM and fails inside a samtools
    call, not with a clear message, when it is missing. Both `x.bam.bai`
    and `x.bai` are accepted on input. The index must sit beside
    `linked_bam` -- the path `_named_link` actually returned (it prefixes
    the requested name with `in_`), not the bare fixed-name constant --
    since that is where samtools looks first.
    """
    for candidate in (Path(f"{raw_bam}.bai"), raw_bam.with_suffix(".bai")):
        if candidate.exists():
            target = Path(f"{linked_bam}.bai")
            if not target.exists():
                target.symlink_to(candidate)
            return
    log.warning("craq_bam_index_missing", bam=str(raw_bam))
