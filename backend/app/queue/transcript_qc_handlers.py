"""RNA-seq transcript QC: gene body coverage and genomic feature distribution.

One job for both charts. They need the same two expensive things -- a parsed
transcript model and a pass over the BAM -- so computing them separately
would parse the GTF twice and traverse the BAM twice for two charts that sit
side by side.
"""

from pathlib import Path

from app.errors import PermanentError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.pipelines import transcript_qc_runner
from app.queue.registry import HandlerMode, JobContext, handler
from app.storage.sequence_stats import DEFAULT_SAMPLE_READS

log = get_logger(__name__)


@handler(
    "run_transcript_qc",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=1, mem_mb=2048, io=IoClass.HEAVY),
    max_attempts=2,
)
def run_transcript_qc(ctx: JobContext) -> dict:
    """Gene body coverage and feature distribution for one RNA-seq BAM.

    Read-only: derives no files, just facts merged onto the object.
    """
    import pysam

    object_id = ctx.payload.get("object_id")
    if not object_id:
        raise PermanentError("run_transcript_qc requires an 'object_id'")

    bam_path = Path(ctx.payload["bam_path"])
    gtf_path = Path(ctx.payload["gtf_path"])

    ctx.progress(phase="gtf", pct=0.1, message="reading the gene annotation")
    with open(gtf_path, errors="replace") as fh:
        transcripts = transcript_qc_runner.parse_gtf_transcripts(fh)
    representatives = transcript_qc_runner.representative_transcripts(transcripts)
    if not representatives:
        raise PermanentError(
            "No usable transcripts in the annotation. Check that it is a GTF "
            "with 'exon' features and gene_id/transcript_id attributes."
        )
    index = transcript_qc_runner.build_feature_index(representatives)

    gene_body = transcript_qc_runner.GeneBodyCoverage()
    features = transcript_qc_runner.FeatureCounts()
    # Transcripts are looked up per contig by position; a dict keyed by
    # contig keeps the gene-body walk from scanning every gene per read.
    by_contig: dict[str, list] = {}
    for t in representatives:
        by_contig.setdefault(t.contig, []).append(t)
    for v in by_contig.values():
        v.sort(key=lambda t: t.span)

    ctx.progress(phase="reads", pct=0.3, message="classifying reads")
    reads = 0
    with pysam.AlignmentFile(str(bam_path), "rb") as af:
        bam_contigs = set(af.references)
        gtf_contigs = {t.contig for t in representatives}
        if transcript_qc_runner.contig_overlap(bam_contigs, gtf_contigs) == 0:
            # Refuse rather than store a plausible-looking 100% intergenic
            # result -- the '1' vs 'chr1' mismatch produces exactly that.
            raise PermanentError(
                "The annotation and the BAM name their contigs differently "
                f"(BAM: {sorted(bam_contigs)[:3]}..., "
                f"annotation: {sorted(gtf_contigs)[:3]}...). "
                "Use an annotation built against the same reference."
            )

        for contig, per_contig_budget in _sampling_plan(af, DEFAULT_SAMPLE_READS):
            taken = 0
            for rec in af.fetch(contig):
                if rec.is_secondary or rec.is_supplementary or rec.is_unmapped:
                    continue
                if rec.is_duplicate:
                    continue
                position = rec.reference_start + 1
                features.add(
                    transcript_qc_runner.classify_position(index, contig, position)
                )
                for t in by_contig.get(contig, ()):
                    start, end = t.span
                    if start <= position <= end:
                        gene_body.add_read(t, position)
                        break
                reads += 1
                taken += 1
                if taken >= per_contig_budget:
                    break

    if reads == 0:
        raise PermanentError("No usable alignments found in this BAM.")

    facts = {
        "transcript_qc_status": "ok",
        "transcript_qc_sampled_reads": reads,
        "transcript_qc_annotation": ctx.payload.get("gtf_name"),
        "gene_body_coverage": gene_body.to_facts(),
        "feature_distribution": features.to_facts(),
    }

    ctx.progress(phase="done", pct=1.0, message="transcript QC complete")
    log.info(
        "transcript_qc_finished",
        job_id=ctx.job_id,
        object_id=object_id,
        reads=reads,
        exonic=facts["feature_distribution"]["exonic"],
    )

    return {
        "object_id": object_id,
        "project_id": ctx.payload.get("project_id"),
        "job_id": ctx.job_id,
        "facts": facts,
    }


def _sampling_plan(af, budget: int) -> list[tuple[str, int]]:
    """How many reads to take from each contig, proportional to its length.

    Reading the first `budget` records instead -- which is what the existing
    alignment stats do -- takes every read from the start of the first contig
    on a coordinate-sorted BAM. For a gene body curve that is a few hundred
    genes on one chromosome, not a genome-wide answer. See issue #191.

    A contig whose proportional share truncates to zero still gets 1 read
    (mirrors bam_stats_runner.allocate_bins's floor-then-distribute
    approach: a contig shorter than one bin still gets one bin, so small
    contigs never vanish from the plan). This is a floor, not an equal
    split -- a scaffold-heavy assembly still spends most of the budget on
    its large contigs -- so the extra spend beyond `budget` is at most one
    read per contig, bounded by the reference's contig count.
    """
    lengths = [(c, af.get_reference_length(c) or 0) for c in af.references]
    total = sum(n for _, n in lengths)
    if total <= 0:
        return [(c, budget) for c, _ in lengths[:1]]
    plan = []
    floored = 0
    for contig, length in lengths:
        if length <= 0:
            continue
        share = int(budget * length / total)
        if share > 0:
            plan.append((contig, share))
        else:
            plan.append((contig, 1))
            floored += 1
    if not plan:
        return [(lengths[0][0], budget)]

    if floored > 0:
        planned_total = sum(share for _, share in plan)
        log.info(
            "transcript_qc_sampling_plan_floored",
            budget=budget,
            planned_total=planned_total,
            contigs=len(plan),
            floored_contigs=floored,
        )
    return plan
