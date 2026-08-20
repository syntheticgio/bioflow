"""binning: separate a metagenome assembly into per-organism bins (MAGs).

Its own module rather than a case in assembly_handlers.py, because this is the
only job in the app that turns one artifact into **N**: the assembly handler's
whole result shape assumes a fixed set of named outputs, and binning's output
count is data-dependent.

Two binaries run here, in order:

  1. `jgi_summarize_bam_contig_depths` -- per-contig mean depth AND variance;
  2. `metabat2` -- the binning itself, reading that depth file.

Step 1 is not substitutable by this app's existing mosdepth coverage job, even
though that job already computes per-contig mean depth. See
metabat_runner.build_depths_command for why: MetaBAT2 bins on coverage
co-variance, and a depth file carrying means alone is one it accepts and bins
worse from without complaint.

The runner underneath (`metabat_runner`) is pure functions only; this module
owns the subprocess calls and the workdir/blob-resolution seam, mirroring the
`mosdepth_runner` / `run_coverage` split.

Imported by `handlers.py` for the `@handler` registration side effects.
"""

from pathlib import Path

from app.config import settings
from app.errors import PermanentError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.pipelines import metabat_runner, tools
from app.queue.align_handlers import _resolve_blob
from app.queue.executor import run_subprocess
from app.queue.pipeline_handlers import _failure, _prepare_workdir
from app.queue.registry import HandlerMode, JobContext, handler

log = get_logger(__name__)


@handler(
    "binning",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    # Binning holds the contig set and its k-mer profiles in memory; the depth
    # step is streaming and cheap by comparison. 8 GB covers a community
    # assembly of a few hundred megabases, which is the scale this app's
    # single-machine posture supports.
    resources=JobResources(cpu=4, mem_mb=8192, io=IoClass.HEAVY),
    # Deterministic failures (too few contigs, a mismatched BAM) do not improve
    # with retries; one retry covers transient disk/exec noise.
    max_attempts=2,
)
def run_binning(ctx: JobContext) -> dict:
    """Bin a metagenome assembly into candidate genomes.

    Returns a *manifest* of bins rather than the bins themselves -- each entry
    a tmp path the applier ingests independently, so a failure on one bin
    cannot cost the others (#728 R3).
    """
    metabat = tools.require(tools.metabat2())
    # Required separately from metabat2 despite shipping in the same package:
    # the job execs it as its own process, so its absence must fail here with
    # its own name rather than surfacing as a confusing metabat2 error about a
    # depth file that was never written.
    tools.require(tools.jgi_depths())

    contigs_id = ctx.payload.get("contigs_id")
    if not contigs_id:
        raise PermanentError("binning requires a 'contigs_id'")

    work = _prepare_workdir(ctx, "binning")

    contigs_name = Path(ctx.payload.get("contigs_name") or "contigs.fasta").name
    contigs = work / contigs_name
    contigs.unlink(missing_ok=True)
    contigs.symlink_to(_resolve_blob(ctx.payload, "contigs"))

    bam_name = Path(ctx.payload.get("bam_name") or "aligned.bam").name
    bam = work / bam_name
    bam.unlink(missing_ok=True)
    bam.symlink_to(_resolve_blob(ctx.payload, "bam"))

    # The depth summarizer seeks per contig and needs the index beside the BAM
    # under the name it expects, not the blob's content-addressed name -- the
    # same linking dance run_coverage does for mosdepth.
    bai = work / f"{bam_name}.bai"
    bai.unlink(missing_ok=True)
    bai.symlink_to(_resolve_blob(ctx.payload, "bai"))

    ctx.progress(phase="depth", pct=0.1, message="summarizing per-contig depth")
    depths = work / "depth.txt"
    depth_log = work / "jgi_depths.log"
    code = run_subprocess(
        ctx,
        metabat_runner.build_depths_command(
            bam=bam, output=depths, jgi_depths=settings.jgi_depths_path
        ),
        log_path=str(depth_log),
    )
    if code != 0:
        raise _failure(code, depth_log, "jgi_summarize_bam_contig_depths")
    if not depths.exists() or depths.stat().st_size == 0:
        # Exit zero with no depth file means the BAM matched none of the
        # contigs -- the usual cause being an alignment against a *different*
        # reference. Named explicitly because MetaBAT2's own error for an
        # empty depth file says nothing about which input was wrong.
        raise PermanentError(
            "The depth summary came out empty. This usually means the "
            "alignment was made against a different reference than the "
            "assembly being binned."
        )

    min_contig = int(ctx.payload.get("min_contig") or 2500)
    threads = int(ctx.payload.get("threads") or 1)
    seed = int(ctx.payload.get("seed") or 1)

    ctx.progress(phase="bin", pct=0.4, message="binning contigs")
    out_prefix = work / "bins" / "bin"
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    bin_log = work / "metabat2.log"
    code = run_subprocess(
        ctx,
        metabat_runner.build_binning_command(
            contigs=contigs,
            depths=depths,
            out_prefix=out_prefix,
            min_contig=min_contig,
            threads=threads,
            seed=seed,
            metabat2=settings.metabat2_path,
        ),
        log_path=str(bin_log),
    )
    if code != 0:
        raise _failure(code, bin_log, "metabat2")

    ctx.progress(phase="collect", pct=0.85, message="collecting bins")
    bins = metabat_runner.enumerate_bins(out_prefix)
    if not bins:
        # A real outcome, not a crash: a low-diversity or shallow sample can
        # leave MetaBAT2 with nothing that clears its cluster-size floor. Said
        # plainly, with the lever that changes it, rather than reported as an
        # unexplained empty result.
        raise PermanentError(
            "MetaBAT2 produced no bins from this assembly. The community may "
            "be too shallow or too uniform to separate, or the minimum contig "
            f"length ({min_contig}) may exclude too much of the assembly."
        )

    # Before anything is ingested. `_apply_binning` re-checks the cap for the
    # same reason, but failing here keeps a run that cannot be applied from
    # occupying a worker slot through its whole apply path.
    try:
        metabat_runner.check_bin_cap(len(bins), settings.metagenome_bin_cap)
    except ValueError as e:
        raise PermanentError(str(e)) from e

    unbinned = metabat_runner.unbinned_path(out_prefix)
    unbinned_contigs, unbinned_bases = (
        metabat_runner.measure_fasta(unbinned) if unbinned else (0, 0)
    )
    excluded = {
        label: metabat_runner.measure_fasta(path)[1]
        for label, path in metabat_runner.excluded_paths(out_prefix).items()
    }

    facts = metabat_runner.binning_facts(
        bins=bins,
        unbinned_bases=unbinned_bases,
        excluded=excluded,
        tool_version=metabat.version,
    )
    if unbinned_contigs:
        facts["binning_unbinned_contig_count"] = unbinned_contigs

    ctx.progress(phase="done", pct=1.0, message=f"{len(bins)} bins")
    log.info(
        "binning_finished",
        job_id=ctx.job_id,
        contigs_id=contigs_id,
        bin_count=len(bins),
        unbinned_bases=unbinned_bases,
    )

    return {
        "object_id": contigs_id,
        "contigs_id": contigs_id,
        "bam_object_id": ctx.payload.get("bam_object_id"),
        "project_id": ctx.payload.get("project_id"),
        "job_id": ctx.job_id,
        "tool_version": metabat.version,
        "params": {
            "min_contig": min_contig,
            "threads": threads,
            "seed": seed,
        },
        # The manifest. One entry per bin, each ingested independently by
        # `_apply_binning` -- deliberately not a single archive, which would
        # make one corrupt bin cost every other.
        "bins": [
            {
                "tmp_path": str(b.path),
                "name": _bin_name(ctx, b.index),
                "index": b.index,
                "contig_count": b.contig_count,
                "total_bases": b.total_bases,
                "mean_depth": b.mean_depth,
            }
            for b in bins
        ],
        "unbinned": (
            {
                "tmp_path": str(unbinned),
                "name": _unbinned_name(ctx),
                "contig_count": unbinned_contigs,
                "total_bases": unbinned_bases,
            }
            if unbinned
            else None
        ),
        "binning_facts": facts,
        "workdir": str(work),
    }


def _assembly_stem(ctx: JobContext) -> str:
    """The assembly's name without its extension, for naming its bins.

    Named after the assembly rather than left as `bin.1.fasta`: a project that
    bins two communities would otherwise hold two sets of identically-named
    MAGs, distinguishable only by opening each one.
    """
    raw = Path(ctx.payload.get("contigs_name") or "assembly").name
    stem = raw
    for suffix in (".gz", ".fasta", ".fa", ".fna"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    return stem or "assembly"


def _bin_name(ctx: JobContext, index: int) -> str:
    # Zero-padded so a project listing sorts bin 2 before bin 10 -- the same
    # lexical-ordering trap enumerate_bins avoids on the filesystem side.
    return f"{_assembly_stem(ctx)}.bin.{index:03d}.fasta"


def _unbinned_name(ctx: JobContext) -> str:
    return f"{_assembly_stem(ctx)}.unbinned.fasta"
