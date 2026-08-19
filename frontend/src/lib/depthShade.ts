/**
 * Turning a contig's mean depth into a colour, relative to a typical contig.
 *
 * Kept out of the component because the reading is the whole point of the
 * chart and the reading lives entirely in these two functions: which ratio a
 * depth maps to, and which end of the scale that ratio lands on. A component
 * test would exercise the SVG; these exercise the claim.
 *
 * The scale is diverging rather than sequential because both directions carry
 * meaning -- half depth is aneuploidy or a sex chromosome at the expected
 * dosage, double depth is a duplication -- and a sequential ramp would make
 * "normal" an arbitrary point partway along it rather than the midpoint a
 * reader can find without consulting a legend.
 */

/**
 * Where the scale saturates, as a multiple of the genome mean.
 *
 * Chosen so the two readings the chart exists for land at the ends rather
 * than partway along: a haploid chromosome in a diploid sample sits at 0.5x,
 * a duplication at 2.0x. Clamping also stops a high-copy plasmid or a
 * mitochondrion -- routinely hundreds of times nuclear depth -- from
 * compressing every chromosome into one indistinguishable shade near the
 * middle.
 */
export const SHADE_MIN_RATIO = 0.5;
export const SHADE_MAX_RATIO = 2.0;

/**
 * Below this the baseline is treated as "nothing aligned" rather than as a
 * number to divide by.
 *
 * A strictly-positive test is not enough, and real data says so. `samtools
 * coverage` reports depth to several decimals, so a BAM with essentially no
 * alignment does not come back as a clean column of zeros -- one in this app
 * (`DRR1078403.bam`, genome mean 0.0) has a median contig depth of
 * 0.000615x, a handful of stray reads. Dividing by that turns rounding noise
 * into ratios spread over 0.7x-1.6x, and the strip paints eight chromosomes
 * as structurally high-depth on a BAM where nothing mapped at all.
 *
 * A hundredth of a read deep is far below any depth anyone would draw a
 * conclusion from, and comfortably below the 1x threshold that
 * `COVERAGE_THRESHOLDS` already calls "sequenced at all".
 */
const MIN_BASELINE_DEPTH = 0.01;

/** Ratios inside this band of 1.0 are drawn neutral. Depth varies by a few
 *  percent between chromosomes in a perfectly ordinary sample, and colouring
 *  that noise invites reading structure into it. */
const NEUTRAL_BAND = 0.1;

export type DepthShade =
  /** At or near the genome mean -- nothing to report. */
  | { kind: "neutral"; ratio: number }
  /** Below the mean. `t` runs 0 (just outside the band) .. 1 (at or past the
   *  clamp), so the component can pick an intensity without redoing the
   *  arithmetic. */
  | { kind: "low"; ratio: number; t: number }
  | { kind: "high"; ratio: number; t: number }
  /** No usable genome mean to compare against, so no reading is possible.
   *  Distinct from "neutral": one says the depth is ordinary, the other says
   *  we cannot say. */
  | { kind: "unknown" };

/**
 * The depth every other contig is read against: the length-weighted median of
 * the per-contig depths.
 *
 * **Not `bam_stats_summary.mean_depth`, and this is the difference between
 * the chart working and the chart lying.** That number is the length-weighted
 * *mean*, which is the right genome-wide summary but the wrong baseline here,
 * because one small very deep sequence moves it arbitrarily far. Measured on
 * a real yeast BAM in this app: the mitochondrion `NC_001224.1` is 86 kb at
 * 8157x -- 0.7% of the genome by length -- and pulls the mean from roughly
 * 26x to 80.19x. Normalizing against that puts all sixteen nuclear
 * chromosomes at 0.18-0.56x, so every one of them renders as a saturated
 * low-depth bar and the strip reports a whole-genome dropout on a perfectly
 * ordinary sample. That is worse than showing nothing.
 *
 * A median cannot be moved by one outlier however extreme, so the same BAM
 * reads as sixteen neutral chromosomes and one very high organelle -- which
 * is what is actually true. Weighted by length so a fragmented assembly's
 * thousands of short scaffolds cannot outvote its real chromosomes.
 *
 * Returns null when there is nothing usable to compare against: no contigs,
 * or a baseline too near zero to divide by (see `MIN_BASELINE_DEPTH`).
 */
export function baselineDepth(
  contigs: readonly { length: number; mean_depth: number }[],
): number | null {
  const usable = contigs.filter(
    (c) =>
      Number.isFinite(c.mean_depth) &&
      c.mean_depth >= 0 &&
      Number.isFinite(c.length) &&
      c.length > 0,
  );
  if (!usable.length) return null;

  const sorted = [...usable].sort((a, b) => a.mean_depth - b.mean_depth);
  const totalLength = sorted.reduce((sum, c) => sum + c.length, 0);

  // The depth at which half the reference's bases sit below and half above.
  let seen = 0;
  let median = sorted[sorted.length - 1].mean_depth;
  for (const c of sorted) {
    seen += c.length;
    if (seen >= totalLength / 2) {
      median = c.mean_depth;
      break;
    }
  }

  return median >= MIN_BASELINE_DEPTH ? median : null;
}

/**
 * A contig's depth as a multiple of the baseline, or null when there is
 * nothing to divide by.
 */
export function depthRatio(
  meanDepth: number,
  baseline: number | undefined | null,
): number | null {
  if (baseline == null || !Number.isFinite(baseline)) return null;
  if (baseline <= 0) return null;
  if (!Number.isFinite(meanDepth) || meanDepth < 0) return null;
  return meanDepth / baseline;
}

/** Classify a ratio onto the diverging scale. */
export function shadeFor(ratio: number | null): DepthShade {
  if (ratio == null) return { kind: "unknown" };

  if (Math.abs(ratio - 1) <= NEUTRAL_BAND) return { kind: "neutral", ratio };

  if (ratio < 1) {
    // Distance from the edge of the neutral band to the clamp, so a contig
    // just outside the band is barely tinted and one at or below 0.5x is
    // fully saturated.
    const span = 1 - NEUTRAL_BAND - SHADE_MIN_RATIO;
    const t = span > 0 ? (1 - NEUTRAL_BAND - ratio) / span : 1;
    return { kind: "low", ratio, t: clamp01(t) };
  }

  const span = SHADE_MAX_RATIO - (1 + NEUTRAL_BAND);
  const t = span > 0 ? (ratio - (1 + NEUTRAL_BAND)) / span : 1;
  return { kind: "high", ratio, t: clamp01(t) };
}

function clamp01(t: number): number {
  if (!Number.isFinite(t)) return 1;
  return Math.min(1, Math.max(0, t));
}

/** How the ratio reads in a tooltip. Written as a sign-free multiple rather
 *  than a percentage difference, matching how depth is quoted everywhere else
 *  in this app. "Typical", not "mean", because the baseline is the median --
 *  see `baselineDepth` for why that distinction is load-bearing. */
export function formatRatio(ratio: number): string {
  return `${ratio.toFixed(2)}× typical depth`;
}
