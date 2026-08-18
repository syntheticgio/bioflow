"""Reference-guided assembly handlers: iVar consensus, Polypolish polishing,
RagTag scaffolding.

All three ride on the reference-based assembly foundation (#21), which
provides validators and a provenance rule but installs no tool and dispatches
to nothing on its own. These handlers are what make it real -- epic #14's
three tool slices.

They answer the foundation's provenance question three different ways, which
is worth knowing before assuming one is a template for the others:

- `consensus_from_alignment` takes a user-supplied BAM, so its launch path
  *validates* that the BAM was aligned to the selected reference.
- `polish_assembly` cannot take a BAM at all -- Polypolish needs every
  location a read maps to, and `align_reads` produces best-alignment output
  -- so it aligns the reads to the draft itself and the target is correct
  *by construction*, recorded as facts rather than checked at launch.
- `scaffold_assembly` follows `polish_assembly`'s shape (RagTag invokes
  minimap2 itself), plus a second provenance obligation neither sibling
  carries: a scaffolded assembly is partly a claim about the *reference*, not
  only the sample, since RagTag names scaffolds after the reference's own
  sequences. See the design doc's "Scaffolds are inference, not observation".

Imported by `handlers.py` for the `@handler` registration side effects.
"""

from pathlib import Path

from app.config import settings
from app.errors import PermanentError, RetryableError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.pipelines import align_runner, ivar_runner, polypolish_runner, ragtag_runner, tools
from app.queue.executor import run_subprocess
from app.queue.pipeline_handlers import _failure, _named_link, _prepare_workdir, _resolve_input
from app.queue.registry import HandlerMode, JobContext, handler

log = get_logger(__name__)

# Viral/amplicon consensus is minutes even at high coverage -- iVar does not
# parallelize and the reference is small. A one-hour lease is generous;
# there is no case here resembling the multi-hour runs
# assembly_qc_handlers.COMPLETENESS_LEASE_SECONDS sizes for.
CONSENSUS_LEASE_SECONDS = 3600


@handler(
    "consensus_from_alignment",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    # 8192, not the 4096 the design first estimated -- corrected 2026-08-05
    # after a real run against a 26Mb T. brucei reference OOM-killed at
    # 4096. `mpileup -d 0` uncaps the pileup depth deliberately (the design's
    # own reasoning: a depth cap silently downsamples amplicon data), which
    # is exactly what makes an unexpectedly high-coverage or repetitive
    # region on a *non-viral* reference expensive. The original "30kb viral
    # reference, small memory" estimate was true for the tool's intended
    # case but this handler is generic over any BAM+reference pair, so the
    # budget has to cover the worst case it can actually be asked to run,
    # not the common one. Matches assess_completeness's budget for a
    # comparable whole-genome operation.
    resources=JobResources(cpu=2, mem_mb=8192, io=IoClass.HEAVY),
    # One attempt, same reasoning assess_completeness gives: the input and
    # the tool are both deterministic, so a retry fails the same way twice.
    max_attempts=1,
)
def consensus_from_alignment(ctx: JobContext) -> dict:
    """Trim primers (if supplied) and call a consensus sequence from a BAM.

    Three stages, one job: `ivar trim` (skipped without a primer BED),
    `samtools sort` on the trimmed BAM (iVar's own output is unsorted, and
    `ivar consensus` needs a position-sorted pileup), then
    `samtools mpileup | ivar consensus` as a single pipefail-wrapped
    invocation. See ivar_runner for why each stage is shaped the way it is.
    """
    tool = tools.require(tools.ivar())
    samtools = tools.require(tools.samtools())

    work = _prepare_workdir(ctx, "consensus")

    bam = _resolve_input(ctx.payload, "bam")
    bam = _named_link(work, bam, ctx.payload.get("bam_name"))

    reference = _resolve_input(ctx.payload, "reference")
    reference = _named_link(work, reference, ctx.payload.get("reference_name"))

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    ctx.extend_lease(CONSENSUS_LEASE_SECONDS)

    primer_bed_name = ctx.payload.get("primer_bed_name")
    primers_trimmed = bool(primer_bed_name)

    consensus_input = bam
    if primers_trimmed:
        ctx.progress(phase="trimming", pct=0.2, message="trimming primers")
        primer_bed = _resolve_input(ctx.payload, "primer_bed")
        primer_bed = _named_link(work, primer_bed, primer_bed_name)

        trim_prefix = work / "trimmed"
        trim_cmd = ivar_runner.build_trim_command(
            ivar_path=tool.path,
            bam=bam,
            primer_bed=primer_bed,
            out_prefix=trim_prefix,
        )
        code = run_subprocess(ctx, trim_cmd, log_path=str(log_path))
        if code != 0:
            raise _failure(code, log_path, "ivar trim")

        trimmed_bam = trim_prefix.with_suffix(".bam")
        if not trimmed_bam.exists():
            raise RetryableError("ivar trim exited successfully but wrote no BAM")

        ctx.progress(phase="sorting", pct=0.4, message="sorting trimmed reads")
        sorted_bam = work / "trimmed.sorted.bam"
        sort_cmd = ivar_runner.build_sort_command(
            samtools_path=samtools.path, bam=trimmed_bam, out=sorted_bam
        )
        sort_progress = align_runner.SamtoolsProgress()
        code = run_subprocess(ctx, sort_cmd, log_path=str(log_path), parser=sort_progress)
        if code != 0:
            raise _failure(code, log_path, "samtools sort")
        consensus_input = sorted_bam

    ctx.progress(phase="consensus", pct=0.7, message="calling consensus")
    out_prefix = work / "consensus"
    params = ivar_runner.ConsensusParams(
        min_quality=ctx.payload.get("min_quality") or 20,
        min_freq=ctx.payload.get("min_freq") if ctx.payload.get("min_freq") is not None else 0.0,
        min_depth=ctx.payload.get("min_depth") or 10,
    )
    consensus_cmd = ivar_runner.build_consensus_command(
        samtools_path=samtools.path,
        ivar_path=tool.path,
        bam=consensus_input,
        reference=reference,
        out_prefix=out_prefix,
        params=params,
    )

    # iVar's own consensus summary goes to stderr, not a file -- verified
    # against a real run, see ivar_runner's module docstring. run_subprocess
    # merges stderr into the same stream as stdout, and passing on_line is
    # the only way to also see the lines rather than only writing them to
    # log_path -- so this collector is what recovers the summary text
    # without duplicating run_subprocess's own cancellation handling.
    output_lines: list[str] = []
    code = run_subprocess(
        ctx, consensus_cmd, log_path=str(log_path), on_line=output_lines.append
    )
    stderr_text = "\n".join(output_lines)
    if code != 0:
        raise _failure(code, log_path, "samtools mpileup | ivar consensus")

    consensus_fasta = out_prefix.with_suffix(".fa")
    if not consensus_fasta.exists() or consensus_fasta.stat().st_size == 0:
        # iVar's exit codes are unreliable -- see the design's risks
        # section -- so an empty or missing FASTA is checked explicitly
        # rather than trusted to a non-zero return code.
        raise RetryableError(
            "ivar consensus exited successfully but produced no sequence"
        )

    facts = ivar_runner.parse_consensus_stderr(stderr_text)
    facts["consensus_tool_version"] = tool.version
    facts["consensus_min_quality"] = params.min_quality
    facts["consensus_min_freq"] = params.min_freq
    facts["consensus_min_depth"] = params.min_depth
    facts["consensus_primers_trimmed"] = primers_trimmed
    ref_len = facts.get("consensus_reference_length")
    zero_depth = facts.get("consensus_zero_depth_positions", 0)
    low_depth = facts.get("consensus_low_depth_positions", 0)
    if ref_len:
        facts["consensus_n_count"] = zero_depth + low_depth
        facts["consensus_ambiguous_pct"] = round(
            100 * (zero_depth + low_depth) / ref_len, 2
        )

    ctx.progress(phase="done", pct=1.0, message="consensus complete")
    log.info(
        "consensus_finished",
        job_id=ctx.job_id,
        primers_trimmed=primers_trimmed,
        n_count=facts.get("consensus_n_count"),
    )

    return {
        "job_id": ctx.job_id,
        "bam_object_id": ctx.payload.get("bam_object_id"),
        "reference_object_id": ctx.payload.get("reference_object_id"),
        "primer_bed_object_id": ctx.payload.get("primer_bed_object_id"),
        "output": {"tmp_path": str(consensus_fasta), "name": "consensus.fasta"},
        "facts": facts,
    }


# Polishing is dominated by the aligner, not by Polypolish. bwa-mem2's index
# is roughly 28 bytes per reference base and it threads well; Polypolish's own
# pass is single-threaded and modest. So the budget is sized for the aligner
# and the polish step rides along inside it.
#
# Worth knowing when reading this job's rows in the memory model: peak RSS
# here describes bwa-mem2's index, so it scales with the *draft*, not with
# read count. A fit that treats it as a function of input bytes is wrong in
# both directions.
POLISH_LEASE_SECONDS = 6 * 3600


@handler(
    "polish_assembly",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=8, mem_mb=16384, io=IoClass.HEAVY),
    # Deterministic tool, deterministic input: a retry fails identically.
    # Same reasoning as consensus_from_alignment and assess_completeness.
    max_attempts=1,
)
def polish_assembly(ctx: JobContext) -> dict:
    """Correct residual base errors in a draft assembly using short reads.

    Five stages, one job: index the draft, align each read file against it
    *separately* with all-alignment output, filter the pair by insert size,
    then polish. See polypolish_runner for why each stage is shaped the way
    it is -- in particular why `-a` is mandatory, why R1 and R2 are aligned
    in separate invocations, and why the SAMs are never sorted.

    The alignment happens here rather than being a user-supplied BAM because
    Polypolish cannot consume one: it needs every location a read maps to,
    and `align_reads` produces best-alignment output. That is also what makes
    this workflow's provenance answer "by construction" rather than "by
    validation" -- the reads are aligned to this draft, so the alignment
    target cannot be anything else. The aligner and its version are recorded
    as facts because nothing else in the object graph witnesses that step.
    """
    tool = tools.require(tools.polypolish())
    aligner = tools.require(tools.bwa_mem2())

    work = _prepare_workdir(ctx, "polish")

    draft = _resolve_input(ctx.payload, "draft")
    draft = _named_link(work, draft, ctx.payload.get("draft_name"))

    reads: list[Path] = []
    for slot in ("reads", "mate"):
        if ctx.payload.get(f"{slot}_object_id") is None:
            continue
        path = _resolve_input(ctx.payload, slot)
        reads.append(_named_link(work, path, ctx.payload.get(f"{slot}_name")))

    if not reads:
        raise PermanentError("polish_assembly requires at least one read file")

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    ctx.extend_lease(POLISH_LEASE_SECONDS)

    threads = max(1, int(ctx.payload.get("threads") or 8))
    params = polypolish_runner.params_for_depth(ctx.payload.get("depth"))

    ctx.progress(phase="indexing", pct=0.05, message="indexing draft assembly")
    code = run_subprocess(
        ctx,
        polypolish_runner.build_index_command(aligner_path=aligner.path, draft=draft),
        log_path=str(log_path),
    )
    if code != 0:
        raise _failure(code, log_path, "bwa-mem2 index")

    sams: list[Path] = []
    for i, read_file in enumerate(reads, start=1):
        ctx.progress(
            phase="aligning",
            pct=0.1 + 0.25 * i,
            message=f"aligning read file {i} of {len(reads)}",
        )
        sam = work / f"alignments_{i}.sam"
        align_cmd = polypolish_runner.build_align_command(
            aligner_path=aligner.path, draft=draft, reads=read_file, threads=threads
        )
        align_progress = align_runner.AlignProgress(name="bwa-mem2")
        code = run_subprocess(
            ctx,
            polypolish_runner.redirect_stdout(align_cmd, sam),
            log_path=str(log_path),
            parser=align_progress,
        )
        if code != 0:
            # Named per stage: a large draft can exhaust memory during the
            # aligner's index build, before Polypolish is ever reached, and
            # "the polish tool failed" would point at the wrong binary.
            raise _failure(code, log_path, f"bwa-mem2 mem (read file {i})")
        sams.append(sam)

    if len(sams) == 2:
        ctx.progress(phase="filtering", pct=0.65, message="filtering by insert size")
        filtered = [work / "filtered_1.sam", work / "filtered_2.sam"]
        code = run_subprocess(
            ctx,
            polypolish_runner.build_filter_command(
                polypolish_path=tool.path, sam_in=sams, sam_out=filtered
            ),
            log_path=str(log_path),
        )
        if code != 0:
            raise _failure(code, log_path, "polypolish filter")
        sams = filtered

    ctx.progress(phase="polishing", pct=0.8, message="polishing assembly")
    polished = work / "polished.fasta"
    polish_cmd = polypolish_runner.build_polish_command(
        polypolish_path=tool.path, draft=draft, sams=sams, params=params
    )
    # Polypolish's per-contig summary goes to stderr, not a file. Same
    # collector shape consensus_from_alignment uses for iVar's summary:
    # run_subprocess merges stderr into the line stream, and on_line is the
    # only way to see those lines rather than only writing them to log_path.
    output_lines: list[str] = []
    code = run_subprocess(
        ctx,
        polypolish_runner.redirect_stdout(polish_cmd, polished),
        log_path=str(log_path),
        on_line=output_lines.append,
    )
    if code != 0:
        raise _failure(code, log_path, "polypolish polish")

    if not polished.exists() or polished.stat().st_size == 0:
        raise RetryableError("polypolish exited successfully but wrote no sequence")

    facts = polypolish_runner.parse_polish_stderr("\n".join(output_lines))
    facts["polish_tool_version"] = tool.version
    facts["polish_aligner"] = aligner.name
    facts["polish_aligner_version"] = aligner.version
    facts["polish_careful_mode"] = params.careful
    facts["polish_read_files"] = len(reads)
    if params.depth is not None:
        facts["polish_estimated_depth"] = round(params.depth, 1)

    ctx.progress(phase="done", pct=1.0, message="polishing complete")
    log.info(
        "polish_finished",
        job_id=ctx.job_id,
        changed=facts.get("polish_changed_positions"),
        careful=params.careful,
    )

    return {
        "job_id": ctx.job_id,
        "draft_object_id": ctx.payload.get("draft_object_id"),
        "reads_object_id": ctx.payload.get("reads_object_id"),
        "mate_object_id": ctx.payload.get("mate_object_id"),
        "output": {"tmp_path": str(polished), "name": "polished.fasta"},
        "facts": facts,
    }


# Bacterial in minutes, a large plant reference can take an hour -- sized
# like the alignment jobs (minimap2's whole-genome alignment dominates the
# cost), not like consensus_from_alignment's small-reference case.
SCAFFOLD_LEASE_SECONDS = 3600


@handler(
    "scaffold_assembly",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    # minimap2's index dominates, the same reasoning polish_assembly's budget
    # gives for bwa-mem2 -- size for the reference, not the draft. LIGHT, not
    # HEAVY: unlike the other two slices there is no high-coverage read file
    # being streamed, just two assemblies. (IoClass has no NORMAL member --
    # NONE/LIGHT/HEAVY is the full set.)
    resources=JobResources(cpu=4, mem_mb=8192, io=IoClass.LIGHT),
    # Deterministic tool, deterministic input -- and see the docstring below
    # for why a retry here would be worse than merely pointless.
    max_attempts=1,
)
def scaffold_assembly(ctx: JobContext) -> dict:
    """Order and orient a draft assembly's contigs against a reference, with
    RagTag.

    One subprocess call. RagTag invokes minimap2 itself -- the alignment is
    not a separate stage the way Polypolish's is, because there is no filter
    step in between; RagTag consumes the alignment directly.

    The critical property of this handler, load-bearing enough to name in
    its own paragraph: **RagTag can exit 0 having produced nothing.** Given
    an unrelated reference it raises `RuntimeError: There are no useful
    alignments`, writes no `ragtag.scaffold.fasta`, and still returns status
    0 -- verified twice against a real 2.1.0 install, see the design doc and
    ragtag_runner's module docstring. So the output file's existence is not
    a belt-and-braces check here, it is the *only* success signal this
    handler has; the subprocess return code is not trustworthy evidence
    either way.
    """
    tool = tools.require(tools.ragtag())

    work = _prepare_workdir(ctx, "scaffold")

    reference = _resolve_input(ctx.payload, "reference")
    reference = _named_link(work, reference, ctx.payload.get("reference_name"))

    draft = _resolve_input(ctx.payload, "draft")
    draft = _named_link(work, draft, ctx.payload.get("draft_name"))

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    ctx.extend_lease(SCAFFOLD_LEASE_SECONDS)

    threads = max(1, int(ctx.payload.get("threads") or 4))
    divergence = ctx.payload.get("divergence") or ragtag_runner.Divergence.SAME_SPECIES

    ctx.progress(phase="scaffolding", pct=0.1, message="aligning and scaffolding")
    out_dir = work / "out"
    cmd = ragtag_runner.build_scaffold_command(
        ragtag_path=tool.path,
        reference=reference,
        draft=draft,
        out_dir=out_dir,
        threads=threads,
        divergence=divergence,
    )
    code = run_subprocess(ctx, cmd, log_path=str(log_path))

    scaffold_fasta = out_dir / "ragtag.scaffold.fasta"
    if not scaffold_fasta.exists() or scaffold_fasta.stat().st_size == 0:
        # Not RetryableError: RagTag's own diagnosis ("no useful alignments")
        # is a statement about these two inputs, and the same pair will fail
        # identically on retry. Surfacing the log path lets the user read
        # RagTag's own message, which is the thing that tells them what to
        # change (a closer reference, or a coarser --mm2-params).
        raise PermanentError(
            "ragtag.py scaffold exited without producing a scaffolded "
            "assembly. This usually means no useful alignments were found "
            f"between the draft and the reference (exit code {code}); see "
            f"{log_path} for RagTag's own diagnosis."
        )

    agp_path = out_dir / "ragtag.scaffold.agp"
    stats = ragtag_runner.parse_stats(
        (out_dir / "ragtag.scaffold.stats").read_text()
        if (out_dir / "ragtag.scaffold.stats").exists()
        else ""
    )
    confidence = ragtag_runner.parse_confidence(
        (out_dir / "ragtag.scaffold.confidence.txt").read_text()
        if (out_dir / "ragtag.scaffold.confidence.txt").exists()
        else ""
    )

    facts = {**stats, **confidence}
    facts["scaffold_tool_version"] = tool.version
    facts["scaffold_aligner"] = "minimap2"
    facts["scaffold_divergence_preset"] = divergence
    facts["scaffold_reference_object_id"] = ctx.payload.get("reference_object_id")
    facts["scaffold_reference_name"] = ctx.payload.get("reference_name")
    facts["scaffold_count"] = ragtag_runner.count_scaffolds(scaffold_fasta.read_text())

    ctx.progress(phase="done", pct=1.0, message="scaffolding complete")
    log.info(
        "scaffold_finished",
        job_id=ctx.job_id,
        placed=facts.get("scaffold_placed_sequences"),
        unplaced=facts.get("scaffold_unplaced_sequences"),
        scaffolds=facts.get("scaffold_count"),
    )

    output = {"tmp_path": str(scaffold_fasta), "name": "scaffolds.fasta"}
    result = {
        "job_id": ctx.job_id,
        "draft_object_id": ctx.payload.get("draft_object_id"),
        "reference_object_id": ctx.payload.get("reference_object_id"),
        "output": output,
        "facts": facts,
    }
    # The AGP is the one intermediate this slice keeps, unlike its siblings:
    # it is the only record of which contig went where and in what
    # orientation, small, and the standard interchange format for exactly
    # this. Optional in the result -- a missing AGP costs a missing sidecar,
    # not a failed job, since the FASTA is the deliverable.
    if agp_path.exists():
        result["agp"] = {"tmp_path": str(agp_path), "name": "scaffolds.agp"}
    return result
