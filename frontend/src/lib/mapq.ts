import type { MapqHistogramBucket } from "../api/types";

/**
 * Telling STAR's MAPQ encoding apart from every other aligner's.
 *
 * bwa-mem2, minimap2, bowtie2 and hisat2 all write a phred-like score in
 * 0-60 (bowtie2 tops out at 42). STAR instead writes a code for how many
 * loci the read was placed at: 255 for a unique placement, then 3, 1 and 0
 * for 2, 3-4 and 5+ loci.
 *
 * The consequence is that a mean MAPQ of ~247 and a mean of ~50 can describe
 * the same reads equally well, and a reader comparing two alignments has no
 * way to see that from the number. So the scale is named wherever a MAPQ is
 * shown, and the mean -- an average over ordinal codes, which is not a
 * quantity at all -- is not shown for STAR.
 */
export const STAR_MAPQ_UNIQUE = 255;

/** What each STAR code says, for labelling the histogram's buckets. */
const STAR_LOCI: Record<number, string> = {
  255: "unique",
  3: "2 loci",
  1: "3–4 loci",
  0: "5+ loci",
};

/**
 * Whether this object's MAPQ values are STAR's codes rather than phred
 * scores.
 *
 * Ingest records `mapq_scale` (see STAR_MAPQ_UNIQUE in sequence_stats.py),
 * but every BAM aligned before that existed has only the histogram, so the
 * histogram is the fallback. A 255 bucket is sufficient on its own: the SAM
 * spec reserves 255 for "mapping quality unavailable", so no phred-scale
 * aligner emits it.
 */
export function isStarMapqScale(facts: Record<string, unknown>): boolean {
  if (facts.mapq_scale === "star") return true;
  const histogram = facts.mapq_histogram as MapqHistogramBucket[] | undefined;
  return !!histogram?.some((b) => b.mapq === STAR_MAPQ_UNIQUE);
}

/**
 * A histogram bucket's axis label. On the STAR scale the code is replaced by
 * what it means, since the numbers themselves order correctly but space
 * absurdly -- 255 sits four times further out than any real quality.
 */
export function mapqBucketLabel(mapq: number, starScale: boolean): string {
  if (!starScale) return `${mapq}`;
  return STAR_LOCI[mapq] ?? `MAPQ ${mapq}`;
}

/** Names the scale for a heading or column header, or "" when conventional. */
export function mapqScaleNote(starScale: boolean): string {
  return starScale ? "STAR scale — 255 = uniquely mapped, not a phred score" : "";
}
