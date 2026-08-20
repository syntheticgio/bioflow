/**
 * Per-chart facts→series extraction for the comparison view.
 *
 * Each chart's overlay needs its props derived from an object's facts, by the
 * *same reading* the single-object panel uses. `ComparisonView` switches on
 * `chart.chartId` to pick an extractor (R7) -- the comparability table gates,
 * this builds the series. Keeping the extraction here, pure and exported,
 * makes it directly testable (the established pattern) and lets the view stay
 * a thin switch over `chartId`.
 *
 * Every extractor returns a discriminated series object (or `null` when the
 * facts are present-but-shapeless), keyed on the same `chartId` so the renderer
 * and the extractor cannot disagree about which chart they belong to.
 */

export type ComparisonSeries =
  | { chartId: "nx"; name: string; curve: [number, number][]; totalBases: number; genomeSize?: number }
  | { chartId: "busco"; name: string; singlePct: number; duplicatedPct: number; fragmentedPct: number; missingPct: number }
  | { chartId: "qc"; name: string; curve: { position: number; mean: number; count: number }[] }
  | { chartId: "depth"; name: string; buckets: { depth: number; count: number }[]; bucketWidth: number };

/** Guard for `quality_per_position`-shaped facts. */
function isQualityCurve(
  v: unknown,
): v is { position: number; mean: number; count: number }[] {
  return Array.isArray(v) && v.every(
    (p) =>
      p &&
      typeof (p as { position?: unknown }).position === "number" &&
      typeof (p as { mean?: unknown }).mean === "number",
  );
}

/** Guard for `bam_stats_depth_histogram`-shaped facts. */
function isDepthBuckets(
  v: unknown,
): v is { depth: number; count: number }[] {
  return Array.isArray(v) && v.every(
    (b) =>
      b &&
      typeof (b as { depth?: unknown }).depth === "number" &&
      typeof (b as { count?: unknown }).count === "number",
  );
}

/** Extract the series for one object+chart, or `null` when facts are absent
 *  or the wrong shape. Mirrors each single-object panel's reading. */
export function extractSeries(
  chartId: string,
  name: string,
  facts: Record<string, unknown>,
): ComparisonSeries | null {
  switch (chartId) {
    case "nx": {
      const curve = facts.sequence_nx_curve;
      const totalBases = facts.total_bases;
      if (
        !Array.isArray(curve) ||
        !curve.every((p) => Array.isArray(p) && p.length === 2) ||
        typeof totalBases !== "number"
      ) {
        return null;
      }
      return {
        chartId: "nx",
        name,
        curve: curve as [number, number][],
        totalBases,
        genomeSize: facts.assembly_genome_size as number | undefined,
      };
    }
    case "busco": {
      const singlePct = facts.assembly_completeness_single_pct;
      const duplicatedPct = facts.assembly_completeness_duplicated_pct;
      const fragmentedPct = facts.assembly_completeness_fragmented_pct;
      const missingPct = facts.assembly_completeness_missing_pct;
      if (
        typeof singlePct !== "number" ||
        typeof duplicatedPct !== "number" ||
        typeof fragmentedPct !== "number" ||
        typeof missingPct !== "number"
      ) {
        return null;
      }
      return {
        chartId: "busco",
        name,
        singlePct,
        duplicatedPct,
        fragmentedPct,
        missingPct,
      };
    }
    case "qc": {
      const curve = facts.quality_per_position;
      if (!isQualityCurve(curve) || curve.length === 0) return null;
      return { chartId: "qc", name, curve };
    }
    case "depth": {
      const buckets = facts.bam_stats_depth_histogram;
      const bucketWidth = facts.bam_stats_depth_bucket_width;
      if (!isDepthBuckets(buckets) || buckets.length === 0) return null;
      if (typeof bucketWidth !== "number" || bucketWidth <= 0) return null;
      return { chartId: "depth", name, buckets, bucketWidth };
    }
    default:
      return null;
  }
}
