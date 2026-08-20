import { describe, expect, it } from "vitest";

import {
  COMPARABLE_CHARTS,
  comparableCharts,
  hasChartFacts,
  type ChartAvailability,
} from "./comparableCharts";

/**
 * The comparability predicate is the only real logic in stage 1, so it is
 * tested directly -- the pure-function pattern for frontend logic in this
 * repo (there is no jsdom setup).
 *
 * The second case is the one that matters: comparability is per-chart, and a
 * pair that differs on one chart must still expose the comparison it *can*
 * have rather than being declared incomparable wholesale.
 */

const NX_FACTS = {
  sequence_nx_curve: [[1, 100], [50, 50], [100, 10]],
  total_bases: 1000,
  assembly_genome_size: 1200, // optional; should not gate the comparison
};

function nxRow(result: ChartAvailability[]) {
  return result.find((r) => r.chart.chartId === "nx");
}

describe("comparableCharts", () => {
  it("reports a chart available when both objects carry the required facts", () => {
    const row = nxRow(
      comparableCharts(NX_FACTS, NX_FACTS, "a", "b"),
    )!;
    expect(row.available).toBe(true);
    expect(row.missing).toEqual([]);
  });

  it("reports a chart unavailable when one object lacks a fact, naming it and the missing fact", () => {
    const { total_bases: _tb, ...b } = NX_FACTS;
    const row = nxRow(comparableCharts(NX_FACTS, b, "A", "B"))!;
    expect(row.available).toBe(false);
    expect(row.missing).toEqual([{ name: "B", facts: ["total_bases"] }]);
  });

  it("reports unavailable with both names when neither object has the facts", () => {
    const row = nxRow(comparableCharts({}, {}, "A", "B"))!;
    expect(row.available).toBe(false);
    expect(row.missing).toEqual([
      { name: "A", facts: ["sequence_nx_curve", "total_bases"] },
      { name: "B", facts: ["sequence_nx_curve", "total_bases"] },
    ]);
  });

  it("treats a null fact as absent, matching the charts' own null guards", () => {
    const b = { ...NX_FACTS, sequence_nx_curve: null };
    const row = nxRow(comparableCharts(NX_FACTS, b, "A", "B"))!;
    expect(row.available).toBe(false);
    expect(row.missing).toEqual([{ name: "B", facts: ["sequence_nx_curve"] }]);
  });

  it("ignores optional facts: an object without genome size still compares", () => {
    const { assembly_genome_size: _gs, ...b } = NX_FACTS;
    const row = nxRow(comparableCharts(NX_FACTS, b, "a", "b"))!;
    expect(row.available).toBe(true);
    expect(row.missing).toEqual([]);
  });
});

describe("COMPARABLE_CHARTS registry", () => {
  // Hand-maintained, keyed by the chartId ComparisonView switches on. A
  // dropped row silently makes that chart unreachable from the UI with no
  // test failing -- so this guards the exact set (AGENTS.md: hand-maintained
  // registries keyed by an enum).
  it("contains exactly the chart ids the comparison view can render", () => {
    expect(COMPARABLE_CHARTS.map((c) => c.chartId).sort()).toEqual([
      "busco",
      "depth",
      "nx",
      "qc",
    ]);
  });

  it("gives every chart a label and at least one required fact", () => {
    for (const chart of COMPARABLE_CHARTS) {
      expect(chart.label.length).toBeGreaterThan(0);
      expect(chart.requiredFacts.length).toBeGreaterThan(0);
    }
  });
});

describe("hasChartFacts", () => {
  it("is true when an object carries every required fact", () => {
    expect(hasChartFacts(NX_FACTS, "nx")).toBe(true);
  });

  it("is false when an object lacks a required fact", () => {
    const { sequence_nx_curve: _c, ...b } = NX_FACTS;
    expect(hasChartFacts(b, "nx")).toBe(false);
  });

  it("is false for an unknown chart id", () => {
    expect(hasChartFacts(NX_FACTS, "does-not-exist")).toBe(false);
  });
});
