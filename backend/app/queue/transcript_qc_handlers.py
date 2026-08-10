"""RNA-seq transcript QC: gene body coverage and genomic feature distribution.

One job for both charts. They need the same two expensive things -- a parsed
transcript model and a pass over the BAM -- so computing them separately
would parse the GTF twice and traverse the BAM twice for two charts that sit
side by side.
"""

import gzip
from pathlib import Path

from app.errors import PermanentError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.pipelines import aligners, transcript_qc_runner
from app.queue.pipeline_handlers import _prepare_workdir
from app.queue.registry import HandlerMode, JobContext, handler
from app.storage.sequence_stats import DEFAULT_SAMPLE_READS

log = get_logger(__name__)


def _is_gzip(path: Path) -> bool:
    """Sniff the gzip magic bytes rather than trusting the `.gtf`/`.gz`
    suffix -- mirrors align_handlers.py's `_is_gzip`. GTFs downloaded from
    NCBI, and files this app compresses on ingest, are routinely gzipped;
    reading gzip bytes with plain `open()` doesn't raise, it just silently
    produces zero valid `exon` lines, which reads as "bad annotation" rather
    than "wrong opener".
    """
    with open(path, "rb") as handle:
        return handle.read(2) == b"\x1f\x8b"


@handler(
    "run_transcript_qc",
    # THREAD, not SUBPROCESS: pysam runs in this process -- there is no
    # binary to spawn or kill via process group.
    mode=HandlerMode.THREAD,
    job_class=JobClass.COMPUTE,
    # Covers the in-memory feature index built from the GTF's exons and
    # genes, plus the per-contig transcript lookup dict, for annotations up
    # to roughly vertebrate-genome scale.
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

    # pysam's .fetch() needs the index next to the BAM, under the name it
    # expects (<name>.bam.bai) -- same convention run_bam_stats uses, since
    # the payload's bam_path/bai_path point at content-addressed blobs with
    # no naming relationship to each other.
    work = _prepare_workdir(ctx, "transcript_qc")
    bam_name = Path(ctx.payload.get("bam_name") or "aligned.bam").name
    bam_path = work / bam_name
    bam_path.unlink(missing_ok=True)
    bam_path.symlink_to(Path(ctx.payload["bam_path"]))

    bai_path = work / f"{bam_name}{aligners.BAI_SUFFIX}"
    bai_path.unlink(missing_ok=True)
    bai_path.symlink_to(Path(ctx.payload["bai_path"]))

    gtf_path = Path(ctx.payload["gtf_path"])

    ctx.progress(phase="gtf", pct=0.1, message="reading the gene annotation")
    opener = gzip.open if _is_gzip(gtf_path) else open
    with opener(gtf_path, "rt", errors="replace") as fh:
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

        for contig, per_contig_budget in transcript_qc_runner.sampling_plan(
            contig_lengths=[
                (c, af.get_reference_length(c) or 0) for c in af.references
            ],
            budget=DEFAULT_SAMPLE_READS,
        ):
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
