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
    gci_runner,
    merqury_runner,
    meryl_runner,
    polypolish_runner,
    quast_runner,
    ragtag_runner,
    synteny_runner,
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


# A single minimap2 whole-genome alignment, no separate report-generation
# step -- the same class of job as assess_misassemblies (QUAST), which also
# runs one external tool once and parses one output file. Matches
# MISASSEMBLY_LEASE_SECONDS's hour rather than assess_completeness's three:
# there is no lineage-scale miniprot/hmmsearch search here, just an alignment.
SYNTENY_LEASE_SECONDS = 3600


@handler(
    "analyze_synteny",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=4, mem_mb=8192, io=IoClass.LIGHT),
    # One attempt: deterministic tool on deterministic input, so a retry
    # fails the same way twice -- same reasoning assess_completeness gives.
    max_attempts=1,
)
def analyze_synteny(ctx: JobContext) -> dict:
    """Whole-genome synteny alignment of a draft assembly against a
    reference, with minimap2.

    Read-only like the other handlers in this module: no new object, only
    facts merged onto the draft assembly that was aligned.

    **minimap2 writes PAF to stdout, and `run_subprocess` cannot capture
    stdout separately from stderr** -- it always redirects both into one
    `log_path` file. `synteny_runner.build_synteny_command` returns a plain
    argv for exactly this reason (see its own docstring), and this handler
    wraps it with `polypolish_runner.redirect_stdout` before running it, the
    same way polypolish's own alignment and polish steps send their stdout
    output to a file instead of the shared log.
    """
    tool = tools.require(tools.minimap2())

    work = _prepare_workdir(ctx, "synteny")

    draft = _resolve_input(ctx.payload, "draft")
    draft = _named_link(work, draft, ctx.payload.get("draft_name"))

    reference = _resolve_input(ctx.payload, "reference")
    reference = _named_link(work, reference, ctx.payload.get("reference_name"))

    divergence = ctx.payload.get("divergence") or ragtag_runner.Divergence.SAME_SPECIES
    threads = max(1, int(ctx.payload.get("threads") or 4))

    cmd = synteny_runner.build_synteny_command(
        minimap2_path=tool.path,
        reference=reference,
        draft=draft,
        divergence=divergence,
        threads=threads,
    )

    paf_path = work / "alignment.paf"
    redirect_cmd = polypolish_runner.redirect_stdout(cmd, paf_path)

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    ctx.progress(phase="starting", pct=None, message="starting minimap2")
    ctx.extend_lease(SYNTENY_LEASE_SECONDS)

    log.info(
        "synteny_started",
        job_id=ctx.job_id,
        reference_object_id=ctx.payload.get("reference_object_id"),
        divergence=divergence,
        threads=threads,
    )

    code = run_subprocess(ctx, redirect_cmd, log_path=str(log_path))
    if code != 0:
        raise _failure(code, log_path, "minimap2")

    if not paf_path.exists():
        # Retryable, the same reasoning assess_misassemblies gives for a
        # missing report.tsv: an exit-0 run with no PAF output is most
        # plausibly a disk that filled during the final write.
        # max_attempts=1 means this will not actually retry -- the class is
        # what tells the user whether re-running is worth their time.
        raise RetryableError("minimap2 exited successfully but wrote no PAF output")

    parsed = synteny_runner.parse_paf(paf_path.read_text(errors="replace"))

    facts = {
        "synteny_alignment": {
            "reference_object_id": ctx.payload.get("reference_object_id"),
            "reference_name": ctx.payload.get("reference_name"),
            "divergence": divergence,
            **parsed,
        }
    }

    ctx.progress(phase="done", pct=1.0, message="synteny alignment complete")
    log.info(
        "synteny_finished",
        job_id=ctx.job_id,
        segments=len(parsed.get("segments") or []),
        partial=parsed.get("synteny_segments_partial", False),
    )

    return {
        "object_id": ctx.payload.get("object_id"),
        "job_id": ctx.job_id,
        "facts": facts,
        "workdir": str(work),
    }


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

_GCI_ASSEMBLY_LINK = "assembly.fasta"
ASSEMBLY_CONTINUITY_LEASE_SECONDS = 3600

# One image per chromosome, so a fragmented assembly produces hundreds of
# files. The launch path (not this handler) gates on this before ever
# setting payload["plot"] -- by the time this handler runs, `plot` in the
# payload is already a decision that's been made, not a check to repeat.
GCI_PLOT_MAX_CONTIGS = 50


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
    samtools call rather than a clear error. BioFlow's storage is
    content-addressed -- a managed BAM and its `.bai` are two unrelated
    `DataObject`s (`SidecarRole.BAI`) with no path relationship between
    them, the same way `launch_bam_stats` resolves `bai_sha256`/`bai_path`
    as their own payload keys rather than guessing a sibling path. The
    launch path is expected to supply `{ngs,sms}_bai_sha256`/`_path`
    alongside the BAM's own; a register-in-place BAM (no sidecar resolved)
    falls back to `.bai`/`with_suffix(".bai")` beside the BAM's own file,
    which is the only case where that guess is valid.
    """
    tool = tools.require(tools.craq())

    work = _prepare_workdir(ctx, "assembly_errors")

    assembly = _resolve_input(ctx.payload, "assembly")
    assembly = _named_link(work, assembly, _CRAQ_ASSEMBLY_LINK)

    ngs_bam = None
    if ctx.payload.get("ngs_bam_path") or ctx.payload.get("ngs_bam_sha256"):
        raw = _resolve_input(ctx.payload, "ngs_bam")
        ngs_bam = _named_link(work, raw, _CRAQ_NGS_LINK)
        _link_bam_index(ctx.payload, "ngs_bai", raw, ngs_bam)

    sms_bam = None
    if ctx.payload.get("sms_bam_path") or ctx.payload.get("sms_bam_sha256"):
        raw = _resolve_input(ctx.payload, "sms_bam")
        sms_bam = _named_link(work, raw, _CRAQ_SMS_LINK)
        _link_bam_index(ctx.payload, "sms_bai", raw, sms_bam)

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
    # `out_final.Report` -- NOT derived from the assembly's filename.
    # Verified against a real 1.10 run on 2026-08-06: all three of
    # `runAQI.sh`/`runAQI_SMS.sh`/`runAQI_NGS.sh` hardcode `name="out"`
    # (line 5 of each), so the report is always `out_final.Report`
    # regardless of what the assembly was linked as or which script ran.
    # The design doc's source read concluded `<genome basename>_final
    # .Report` from `runAQI.sh`'s use of `$name` without confirming what
    # `$name` actually resolves to -- a real run is what caught this, the
    # same way it caught the two `_named_link`-prefix bugs in Task 3.
    report_path = aqi_dir / "out_final.Report"
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
        # So the results applier can attribute a corrected FASTA to the BAM(s)
        # that produced it (ingest_local_file's derived_from), the same ids
        # the launch path stamped into the payload -- see
        # launch_assembly_error_qc's `payload[f"{prefix}_object_id"]`.
        "ngs_bam_object_id": ctx.payload.get("ngs_bam_object_id"),
        "sms_bam_object_id": ctx.payload.get("sms_bam_object_id"),
    }


def _link_bam_index(payload: dict, prefix: str, raw_bam: Path, linked_bam: Path) -> None:
    """Link a BAM's `.bai` beside its fixed-name link.

    CRAQ requires the index next to the BAM and fails inside a samtools
    call, not with a clear message, when it is missing -- so a missing
    index is a `PermanentError` here, not a warning: this handler already
    knows the failure will be opaque, and letting it happen anyway just
    trades a clear message for an obscure one.

    Prefers `payload[f"{prefix}_sha256"/"_path"]`, resolved the same way
    `_resolve_input` resolves the BAM itself -- BioFlow's storage is
    content-addressed, so a managed BAM's `.bai` is a sibling `DataObject`
    with no path relationship to the BAM's own blob path, and the launch
    path is expected to supply it explicitly (see `launch_bam_stats`'s
    `bai_sha256`/`bai_path`, the existing precedent for this exact
    resolve-the-sidecar-separately shape). Falls back to `x.bam.bai` /
    `x.bai` beside `raw_bam` only when the payload carries no index at
    all -- valid for a register-in-place file already sitting under its
    real name, not for one from BioFlow's own storage.
    """
    if payload.get(f"{prefix}_sha256") or payload.get(f"{prefix}_path"):
        source = _resolve_input(payload, prefix)
    else:
        source = next(
            (
                c
                for c in (Path(f"{raw_bam}.bai"), raw_bam.with_suffix(".bai"))
                if c.exists()
            ),
            None,
        )
        if source is None:
            raise PermanentError(
                f"{raw_bam.name} has no discoverable .bai index; CRAQ cannot "
                "run without one"
            )

    target = Path(f"{linked_bam}.bai")
    if not target.exists():
        target.symlink_to(source)


_MERQURY_ASSEMBLY_LINK = "assembly.fasta"
_MERQURY_READ_DB_LINK = "reads.meryl"
ASSEMBLY_QV_LEASE_SECONDS = 3600
# The version installed by install-merqury.sh -- merqury.sh has no --version
# flag, so this is the only accurate "version" available; see the comment
# where it is used below.
MERQURY_PINNED_VERSION = "1.4.1"


@handler(
    "assess_assembly_qv",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=4, mem_mb=12288, io=IoClass.HEAVY),
    max_attempts=1,
)
def assess_assembly_qv(ctx: JobContext) -> dict:
    """Reference-free base-level accuracy (QV) for one assembly, with Merqury.

    Read-only: no new object, only facts merged onto the assembly that was
    scored, plus spectra-cn plots written under qc_reports/.

    **Input filenames never reach the command line.** merqury.sh derives
    every output filename from its input basenames, so an object named
    `ev<img src=x>.fasta` would otherwise put that string into an output
    path -- the same shape as the stored XSS QUAST's slice found, and
    prevented here the same way: every input is linked under a fixed name.

    **The read database may arrive prebuilt.** When the launch path resolved
    a cached MERYL_DB sidecar for this read set at this k, `read_db_path` is
    set and this handler skips the `meryl count`. Otherwise it builds one and
    reports its location in the result so the applier can ingest it as a
    sidecar for the next run. Rebuilding per assembly is the wasteful
    default this cache exists to avoid.

    **mem_mb is 12288, not CRAQ's 8192.** Measured against a real run
    (2026-08-07): S. cerevisiae R64 (~12 Mb genome) against DRR1066343
    (23.7 billion input bases, 21.4M reads) peaked at 8531324928 bytes
    (~8.14 GiB) RSS and took 5m50s wall time. 12288 keeps ~4 GiB of headroom
    above that measured peak -- the plan's original 16384 placeholder was
    directionally reasonable but untested; this replaces the guess with a
    measured figure plus margin, per the plan's own instruction to correct
    this against real data rather than trust the placeholder.
    """
    meryl_tool = tools.require(tools.meryl())
    merqury_tool = tools.require(tools.merqury())

    work = _prepare_workdir(ctx, "assembly_qv")

    assembly = _resolve_input(ctx.payload, "assembly")
    assembly = _named_link(work, assembly, _MERQURY_ASSEMBLY_LINK)

    k = int(ctx.payload.get("k") or 21)
    threads = max(1, int(ctx.payload.get("threads") or 4))

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    read_db = work / _MERQURY_READ_DB_LINK
    built_read_db = False

    cached = ctx.payload.get("read_db_path")
    if cached:
        _link_tree(Path(cached), read_db)
    else:
        read_files = _resolve_read_inputs(work, ctx.payload)
        if not read_files:
            raise PermanentError(
                "QV assessment needs the reads this assembly was built from."
            )
        ctx.progress(phase="counting", pct=None, message="building k-mer database")
        ctx.extend_lease(ASSEMBLY_QV_LEASE_SECONDS)
        count_cmd = merqury_runner.build_meryl_count_command(
            meryl_path=meryl_tool.path,
            k=k,
            reads=read_files,
            output=read_db,
            threads=threads,
        )
        code = run_subprocess(ctx, count_cmd, log_path=str(log_path))
        if code != 0:
            raise _failure(code, log_path, "meryl")
        built_read_db = True

    out_dir = work / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = merqury_runner.build_merqury_command(
        merqury_path=merqury_tool.path,
        read_db=read_db,
        assembly=assembly,
        out_prefix="qv",
    )

    ctx.progress(phase="scoring", pct=None, message="starting merqury")
    ctx.extend_lease(ASSEMBLY_QV_LEASE_SECONDS)

    log.info(
        "assembly_qv_started",
        job_id=ctx.job_id,
        k=k,
        read_db_cached=not built_read_db,
        threads=threads,
    )

    code = run_subprocess(ctx, cmd, log_path=str(log_path), cwd=str(out_dir))
    if code != 0:
        raise _failure(code, log_path, "merqury")

    qv_file = out_dir / "qv.qv"
    completeness_file = out_dir / "qv.completeness.stats"
    if not qv_file.exists():
        raise _failure(code, log_path, "merqury")

    facts = merqury_runner.parse_qv(qv_file.read_text())
    if completeness_file.exists():
        facts.update(merqury_runner.parse_completeness(completeness_file.read_text()))

    facts.update(
        {
            "assembly_qv_k": k,
            "assembly_qv_read_object_id": str(ctx.payload.get("read_object_id") or ""),
            "assembly_qv_read_object_name": str(
                ctx.payload.get("read_object_name") or ""
            ),
            "assembly_qv_tool": "merqury",
            # merqury.sh has no --version: tools.merqury()'s probe (see its
            # docstring) reports its bare-call usage banner as `version`
            # because that is the only output the tool ever produces, not
            # because it is a version string. Confirmed against a real run:
            # storing it verbatim put "Usage: merqury.sh [-c] <read-db..."
            # in this fact. MERQURY_PINNED_VERSION is the version this image
            # actually installs (see install-merqury.sh), which is the only
            # true "version" available here.
            "assembly_qv_tool_version": MERQURY_PINNED_VERSION,
            "assembly_qv_meryl_version": meryl_tool.version or "",
        }
    )

    report_dir = settings.qc_reports_dir / str(ctx.payload["object_id"])
    report_dir.mkdir(parents=True, exist_ok=True)
    for png in out_dir.glob("*.png"):
        shutil.copy2(png, report_dir / png.name)

    ctx.progress(phase="done", pct=1.0, message="QV assessment complete")
    log.info(
        "assembly_qv_finished",
        job_id=ctx.job_id,
        qv=facts.get("assembly_qv"),
        completeness=facts.get("assembly_qv_completeness_pct"),
    )

    result = {
        "object_id": ctx.payload["object_id"],
        "job_id": ctx.job_id,
        "facts": facts,
        "read_object_id": ctx.payload.get("read_object_id"),
        "k": k,
    }
    if built_read_db:
        # The applier (Task 4's other half, in results.py) ingests each file
        # inside this directory as its own MERYL_DB sidecar on the read
        # object -- see _apply_assess_assembly_qv for exactly what shape it
        # expects here.
        result["read_db_dir"] = str(read_db)
    return result


def _link_tree(source: Path, dest: Path) -> None:
    """Link a meryl database directory into the work dir under a fixed name.

    A meryl database is a directory, not a file, so `_named_link`'s
    single-file symlink does not apply. A symlink to the directory is
    enough: meryl reads it and never writes into it during a QV run.
    """
    if dest.exists() or dest.is_symlink():
        dest.unlink()
    dest.symlink_to(source, target_is_directory=True)


def _resolve_read_inputs(work: Path, payload: dict) -> list[Path]:
    """Every read file in the set, linked under fixed sequential names.

    Paired-end reads are two files whose k-mers belong in one database --
    the QV denominator is the whole read set, not one mate.

    Each entry is its own mini-payload with `read_path`/`read_sha256` keys,
    so `_resolve_input` applies per entry. Fixed sequential names keep any
    object's own name off the command line, the same reason the assembly
    gets a fixed link.

    **The fixed name preserves the source's real compression suffix.**
    meryl detects gzip by file extension, the same convention
    `align_runner._is_gzipped` already relies on for aligner inputs -- a
    plain FASTQ forced under a `.fastq.gz` name is not decompressed, and
    `meryl count` silently built an empty database from one rather than
    erroring, confirmed against a real run. `entry["read_name"]` is the
    source object's own name; only its suffix is trusted here; the object's
    own basename never reaches the command line, matching the assembly's
    fixed-link treatment above.
    """
    resolved: list[Path] = []
    for i, entry in enumerate(payload.get("reads") or []):
        raw = _resolve_input(entry, "read")
        source_name = str(entry.get("read_name") or "")
        suffix = "".join(Path(source_name).suffixes) or ".fastq"
        resolved.append(_named_link(work, raw, f"reads_{i}{suffix}"))
    return resolved


def _resolve_gci_bam_entry(work: Path, index: int, slot: str, entry: dict) -> Path:
    """Link one BAM from a GCI `{hifi,nano}_bams` list entry, plus its `.bai`.

    Named `<slot>.<index>.bam` rather than a fixed `hifi.bam`/`nano.bam`:
    two BAMs in one slot (minimap2 + winnowmap, cross-checking each other)
    cannot both link to the same name. `index` is the entry's position in
    the payload list, not tied to which aligner produced it -- order is
    otherwise unobserved by GCI, which takes the whole `--hifi`/`--nano`
    argument list as one undifferentiated set of alignments.
    """
    raw = _resolve_input(entry, "bam")
    linked = _named_link(work, raw, f"{slot}.{index}.bam")
    _link_bam_index(entry, "bai", raw, linked)
    return linked


@handler(
    "assess_assembly_continuity",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=8, mem_mb=16384, io=IoClass.HEAVY),
    max_attempts=1,
)
def assess_assembly_continuity(ctx: JobContext) -> dict:
    """Long-read continuity inspection for one assembly, with GCI.

    Read-only: no new object, only facts merged onto the assembly that was
    scored, plus optional depth plots under qc_reports/.

    **GCI runs no aligner.** It consumes the sorted, indexed BAMs the align
    pipeline already produced. A QC job that silently ran minimap2 or
    winnowmap would duplicate work the user can see and make the job's cost
    unpredictable.

    **Each slot is a list, because GCI's `--hifi`/`--nano` are `nargs='+'`.**
    Verified against the installed `/opt/gci/GCI.py:1041-1042`: both flags
    take one or more BAM paths natively, so pairing minimap2 with winnowmap
    needs no merge step -- both BAMs go on the same flag. `hifi_bams` and
    `nano_bams` are each a list of `{sha256|path, bai_sha256|bai_path}`
    dicts, resolved and linked by `_resolve_gci_bam_entry`.

    **Each BAM's .bai must be linked beside it.** Storage is
    content-addressed, so a managed BAM and its index are two unrelated
    DataObjects; the launch path supplies each entry's `bai_sha256`/`_path`
    the way `launch_bam_stats` does, and a register-in-place BAM falls back
    to a sibling `.bai`, which is the only case where that guess is valid.
    GCI's README says of the index: "this is necessary!!!"

    **The aligners are derived from what was actually linked, not asserted
    by the payload.** Each entry names the object id it came from, and this
    handler reads that object's `aligned_by` fact rather than trusting a
    payload-level `aligners` list -- a payload claiming
    `["minimap2", "winnowmap"]` while only one BAM actually reached the
    command line would otherwise store a score labeled as cross-checked
    when it was not, which is the whole point of the field.
    """
    tool = tools.require(tools.gci())

    work = _prepare_workdir(ctx, "assembly_continuity")

    assembly = _resolve_input(ctx.payload, "assembly")
    assembly = _named_link(work, assembly, _GCI_ASSEMBLY_LINK)

    hifi_entries = ctx.payload.get("hifi_bams") or []
    nano_entries = ctx.payload.get("nano_bams") or []

    hifi_bams = [
        _resolve_gci_bam_entry(work, i, "hifi", entry)
        for i, entry in enumerate(hifi_entries)
    ]
    nano_bams = [
        _resolve_gci_bam_entry(work, i, "nano", entry)
        for i, entry in enumerate(nano_entries)
    ]

    if not hifi_bams and not nano_bams:
        raise PermanentError(
            "Continuity inspection needs long reads aligned to this assembly."
        )

    threads = max(1, int(ctx.payload.get("threads") or 8))
    map_qual = int(ctx.payload.get("map_qual") or 30)
    mq_cutoff = int(ctx.payload.get("mq_cutoff") or gci_runner.DEFAULT_MQ_CUTOFF)
    ovlp_percent = float(
        ctx.payload.get("ovlp_percent") or gci_runner.DEFAULT_OVLP_PERCENT
    )
    plot = bool(ctx.payload.get("plot"))

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    out_dir = work / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = gci_runner.build_gci_command(
        gci_path=tool.path,
        assembly=assembly,
        hifi_bams=hifi_bams,
        nano_bams=nano_bams,
        out_dir=out_dir,
        prefix="gci",
        threads=threads,
        map_qual=map_qual,
        mq_cutoff=mq_cutoff,
        ovlp_percent=ovlp_percent,
        plot=plot,
    )

    ctx.progress(phase="starting", pct=None, message="starting gci")
    ctx.extend_lease(ASSEMBLY_CONTINUITY_LEASE_SECONDS)

    log.info(
        "assembly_continuity_started",
        job_id=ctx.job_id,
        hifi_bams=len(hifi_bams),
        nano_bams=len(nano_bams),
        map_qual=map_qual,
        plot=plot,
        threads=threads,
    )

    code = run_subprocess(ctx, cmd, log_path=str(log_path))
    if code != 0:
        raise RetryableError(f"gci exited {code}; see {log_path}")

    gci_file = out_dir / "gci.gci"
    if not gci_file.exists():
        raise RetryableError(f"gci produced no gci.gci in {out_dir}; see {log_path}")

    facts = gci_runner.parse_gci(gci_file.read_text())

    # `aligned_by` from each entry, in the order BAMs actually reached the
    # command line -- sorted and deduplicated so the value describes *which*
    # aligners cross-checked, not how many BAMs of each ran. An entry with
    # no `aligned_by` (a register-in-place import predating that field, or
    # one that genuinely was never recorded) contributes "unknown" rather
    # than being silently skipped or guessed as "minimap2" -- a guess here
    # is the same class of error this whole derivation replaces.
    aligned_by = sorted(
        {
            str(entry.get("aligned_by") or "unknown")
            for entry in (*hifi_entries, *nano_entries)
        }
    )
    facts.update(
        {
            "assembly_continuity_aligners": aligned_by,
            "assembly_continuity_map_qual": map_qual,
            "assembly_continuity_threshold": int(ctx.payload.get("threshold") or 0),
            "assembly_continuity_tool": "gci",
            "assembly_continuity_tool_version": tool.version or "",
        }
    )
    if len(hifi_bams) > 1 or len(nano_bams) > 1:
        facts["assembly_continuity_mq_cutoff"] = mq_cutoff
        facts["assembly_continuity_ovlp_percent"] = ovlp_percent

    if plot:
        report_dir = settings.qc_reports_dir / str(ctx.payload.get("object_id"))
        report_dir.mkdir(parents=True, exist_ok=True)
        images = out_dir / "images"
        if images.is_dir():
            for image in images.iterdir():
                if image.suffix in {".pdf", ".png"}:
                    shutil.copy2(image, report_dir / image.name)

    ctx.progress(phase="done", pct=1.0, message="continuity scored")
    log.info(
        "assembly_continuity_finished",
        job_id=ctx.job_id,
        gci=facts.get("assembly_continuity_gci"),
    )

    return {
        "object_id": ctx.payload.get("object_id"),
        "job_id": ctx.job_id,
        "facts": facts,
        "workdir": str(work),
    }


# GC tracks for a genome assembly is a pure-Python scan — no subprocess,
# no external tool.  A full scan of a large genome can run for tens of
# minutes, so a lease extension is appropriate.
_GC_TRACKS_LEASE_SECONDS = 7200  # 2h


@handler(
    "analyze_gc_tracks",
    mode=HandlerMode.THREAD,
    job_class=JobClass.COMPUTE,
    # One core: the scan is single-threaded.  Modest memory: one contig
    # buffered at a time.  Heavy IO: it reads the entire file, unlike the
    # sampler in sequence_stats which spends a fixed byte budget.
    resources=JobResources(cpu=1, mem_mb=2048, io=IoClass.HEAVY),
    max_attempts=1,
)
def analyze_gc_tracks(ctx: JobContext) -> dict:
    """Scan an assembly FASTA and compute per-contig GC content and skew
    tracks for a Circos plot.

    This is a THREAD handler — pure Python, no subprocess — so it must
    not call asyncio.run().  If it ever needs to touch Mongo through an
    async path, use app.db.client.run_from_thread.
    """
    from pathlib import Path

    from app.models import Compression
    from app.pipelines.gc_tracks import compute_gc_tracks

    assembly = _resolve_input(ctx.payload, "assembly")
    compression_raw = ctx.payload.get("compression") or "none"
    try:
        compression = Compression(compression_raw)
    except ValueError:
        compression = Compression.NONE

    ctx.progress(phase="starting", pct=None, message="scanning assembly for GC tracks")
    ctx.extend_lease(_GC_TRACKS_LEASE_SECONDS)

    log.info("gc_tracks_started", job_id=ctx.job_id, path=str(assembly))

    result = compute_gc_tracks(Path(assembly), compression, cancel_event=ctx.cancel_event)

    ctx.progress(phase="done", pct=1.0, message="GC tracks computed")
    log.info(
        "gc_tracks_finished",
        job_id=ctx.job_id,
        contigs=len(result.get("contigs") or []),
        partial=result.get("gc_tracks_partial"),
    )

    if result and result.get("contigs"):
        return {
            "object_id": ctx.payload.get("object_id"),
            "job_id": ctx.job_id,
            "facts": {"gc_tracks": result},
        }
    return {
        "object_id": ctx.payload.get("object_id"),
        "job_id": ctx.job_id,
        "facts": {},
    }


# ── meryl k-mer spectra and repeat density ────────────────────────────

_MERYL_TRACKS_LEASE_SECONDS = 7200  # 2h — two meryl count runs on a large genome


@handler(
    "analyze_meryl_tracks",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=4, mem_mb=8192, io=IoClass.HEAVY),
    max_attempts=1,
)
def analyze_meryl_tracks(ctx: JobContext) -> dict:
    """Run meryl k-mer spectra and repeat-density analysis on an assembly.

    Two analyses in one job: a k-mer frequency spectrum from the reads
    (genome size, heterozygosity) and a per-window repeat-density track
    from the assembly (for the Circos plot's repeat-density ring).

    SUBPROCESS — meryl is an external tool. Returns two facts:
    ``kmer_spectra`` and ``repeat_density``.
    """
    meryl_tool = tools.require(tools.meryl())
    k = int(ctx.payload.get("k") or 21)
    threads = max(1, int(ctx.payload.get("threads") or 4))

    work = _prepare_workdir(ctx, "meryl_tracks")
    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    facts: dict = {}
    read_object_name = str(ctx.payload.get("read_object_name") or "")

    # ── Step 1: k-mer spectra from reads ──────────────────────────────

    read_db = work / "reads.meryl"
    built_read_db = False

    cached = ctx.payload.get("read_db_path")
    if cached:
        _link_tree(Path(cached), read_db)
    else:
        read_files = _resolve_read_inputs(work, ctx.payload)
        if not read_files:
            log.warning("meryl_tracks_no_reads", job_id=ctx.job_id)
        else:
            ctx.progress(phase="counting reads", pct=None, message="building k-mer database")
            ctx.extend_lease(_MERYL_TRACKS_LEASE_SECONDS)
            count_cmd = merqury_runner.build_meryl_count_command(
                meryl_path=meryl_tool.path,
                k=k,
                reads=read_files,
                output=read_db,
                threads=threads,
            )
            code = run_subprocess(ctx, count_cmd, log_path=str(log_path))
            if code != 0:
                raise _failure(code, log_path, "meryl")
            built_read_db = True

    if read_db.exists():
        ctx.progress(phase="spectra", pct=None, message="computing k-mer spectra")
        ctx.extend_lease(_MERYL_TRACKS_LEASE_SECONDS)
        stats_cmd = meryl_runner.build_meryl_statistics_command(
            meryl_path=meryl_tool.path,
            database=read_db,
        )
        code = run_subprocess(ctx, stats_cmd, log_path=str(log_path))
        if code == 0:
            raw = log_path.read_text()
            histogram = meryl_runner.parse_meryl_histogram(raw)
            if histogram:
                spectra = meryl_runner.compute_genome_size(histogram, k=k)
                spectra["k"] = k
                spectra["read_set_name"] = read_object_name
                facts["kmer_spectra"] = spectra

    # ── Step 2: repeat density from assembly ──────────────────────────

    assembly = _resolve_input(ctx.payload, "assembly")
    asm_db = work / "assembly.meryl"

    ctx.progress(phase="counting assembly", pct=None, message="building assembly k-mer database")
    ctx.extend_lease(_MERYL_TRACKS_LEASE_SECONDS)
    count_cmd = merqury_runner.build_meryl_count_command(
        meryl_path=meryl_tool.path,
        k=k,
        reads=[assembly],
        output=asm_db,
        threads=threads,
    )
    code = run_subprocess(ctx, count_cmd, log_path=str(log_path))
    if code != 0:
        raise _failure(code, log_path, "meryl")

    ctx.progress(phase="repeat density", pct=None, message="computing repeat density")
    ctx.extend_lease(_MERYL_TRACKS_LEASE_SECONDS)
    print_cmd = meryl_runner.build_meryl_print_gt_command(
        meryl_path=meryl_tool.path,
        database=asm_db,
    )
    code = run_subprocess(ctx, print_cmd, log_path=str(log_path))
    if code != 0:
        raise _failure(code, log_path, "meryl")

    # Read sequence_lengths fact to get contig lengths for windowing.
    sequence_lengths_raw = ctx.payload.get("sequence_lengths")
    if sequence_lengths_raw and isinstance(sequence_lengths_raw, dict):
        contig_lengths = {
            name: int(length)
            for name, length in sequence_lengths_raw.items()
        }
    else:
        contig_lengths = {}

    kmer_lines = log_path.read_text().splitlines()
    density = meryl_runner.compute_repeat_density(kmer_lines, contig_lengths)
    if density and density.get("contigs"):
        facts["repeat_density"] = density

    ctx.progress(phase="done", pct=1.0, message="meryl analysis complete")
    log.info(
        "meryl_tracks_finished",
        job_id=ctx.job_id,
        spectra=("kmer_spectra" in facts),
        repeat_density=("repeat_density" in facts),
    )

    return {
        "object_id": ctx.payload.get("object_id"),
        "job_id": ctx.job_id,
        "facts": facts,
    }
