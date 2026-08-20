/**
 * What can be compared between two objects, computed per chart.
 *
 * The comparison view (`ComparisonView`) renders one overlay per chart that
 * both objects can carry. This module is the single source of truth for "can
 * this chart be drawn for this pair", and for the reason it cannot be when it
 * cannot -- both derive from the same table, so adding a comparable chart is
 * one row here and a renderer in `ComparisonView`, and the "why is this
 * unavailable" message can never drift from the gate that produced it.
 *
 * Comparability is over *facts*, not formats or roles (spec C3): two
 * assemblies are comparable on Nx iff both carry `sequence_nx_curve` and
 * `total_bases`, regardless of which tools assembled them. That degrades
 * correctly -- a pair with no shared chart-backing facts is comparable on
 * nothing, and the view says so rather than guessing.
 */

export interface ComparableChart {
  /** Stable id; the key a renderer in `ComparisonView` switches on. */
  chartId: string;
  /** Human label, used as the section heading and in the picker. */
  label: string;
  /** Fact keys whose presence (non-null) on an object makes the chart
   *  drawable for it. Optional facts like `assembly_genome_size` (which only
   *  enable the NGx curve) are deliberately excluded: the Nx overlay is the
   *  comparison, and an object lacking the optional fact still compares. */
  requiredFacts: string[];
}

/** The comparability table. Stage 1 holds one row; stage 2 adds rows here. */
export const COMPARABLE_CHARTS: ComparableChart[] = [
  {
    chartId: "nx",
    label: "Nx contiguity",
    requiredFacts: ["sequence_nx_curve", "total_bases"],
  },
  {
    chartId: "busco",
    label: "BUSCO completeness",
    requiredFacts: [
      "assembly_completeness_single_pct",
      "assembly_completeness_duplicated_pct",
      "assembly_completeness_fragmented_pct",
      "assembly_completeness_missing_pct",
    ],
  },
  {
    chartId: "qc",
    label: "Per-base quality",
    requiredFacts: ["quality_per_position"],
  },
  {
    chartId: "depth",
    label: "Sequencing depth",
    requiredFacts: ["bam_stats_depth_histogram", "bam_stats_depth_bucket_width"],
  },
];

export interface ChartAvailability {
  chart: ComparableChart;
  /** True when both objects carry every required fact. */
  available: boolean;
  /** When unavailable, which object lacks what. Empty when available. */
  missing: { name: string; facts: string[] }[];
}

/**
 * Which required facts are present on an object's facts dict.
 */
function presentFacts(
  facts: Record<string, unknown>,
  required: string[],
): string[] {
  return required.filter((f) => facts[f] != null);
}

/**
 * The comparability verdict for every chart in the table, for one object pair.
 *
 * Per-chart, not per-pair: two assemblies may overlay on Nx while only one has
 * a BUSCO run, and collapsing that to a pair verdict would hide the comparison
 * the user can actually have. `missing` names the object and the facts it
 * lacks, so the view can say *why* a chart is unavailable rather than just
 * that it is.
 */
export function comparableCharts(
  factsA: Record<string, unknown>,
  factsB: Record<string, unknown>,
  nameA = "Object A",
  nameB = "Object B",
): ChartAvailability[] {
  return COMPARABLE_CHARTS.map((chart) => {
    const a = presentFacts(factsA, chart.requiredFacts);
    const b = presentFacts(factsB, chart.requiredFacts);
    const missingA = chart.requiredFacts.filter((f) => !a.includes(f));
    const missingB = chart.requiredFacts.filter((f) => !b.includes(f));
    const missing: { name: string; facts: string[] }[] = [];
    if (missingA.length > 0) missing.push({ name: nameA, facts: missingA });
    if (missingB.length > 0) missing.push({ name: nameB, facts: missingB });
    return { chart, available: missing.length === 0, missing };
  });
}

/**
 * Whether a given object carries the facts for a chart id -- used to filter
 * the "compare with…" picker to objects that share at least one comparable
 * chart with the object being viewed (R1).
 */
export function hasChartFacts(
  facts: Record<string, unknown>,
  chartId: string,
): boolean {
  const chart = COMPARABLE_CHARTS.find((c) => c.chartId === chartId);
  if (!chart) return false;
  return presentFacts(facts, chart.requiredFacts).length ===
    chart.requiredFacts.length;
}
