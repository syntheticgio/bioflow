import { describe, expect, it } from "vitest";

import { extractSeries } from "./comparisonSeries";

/**
 * Per-chart facts→series extraction: pure over a fact dict, so tested
 * directly (no jsdom), the established pattern. Each chart must read the
 * same facts the single-object panel does (so the comparison cannot drift),
 * and return `null` when facts are absent or the wrong shape -- which is what
 * the `missing` messaging in the view is built from.
 */

const NAME = "assembly.fna";

describe("nx series", () => {
  it("reads sequence_nx_curve, total_bases, and optional assembly_genome_size", () => {
    const s = extractSeries("nx", NAME, {
      sequence_nx_curve: [[1, 100]],
      total_bases: 200_000,
      assembly_genome_size: 210_000,
    });
    expect(s).toEqual({
      chartId: "nx",
      name: NAME,
      curve: [[1, 100]],
      totalBases: 200_000,
      genomeSize: 210_000,
    });
  });

  it("is null when a required fact is missing", () => {
    expect(extractSeries("nx", NAME, { total_bases: 200_000 })).toBeNull();
    expect(extractSeries("nx", NAME, { sequence_nx_curve: [[1, 100]] })).toBeNull();
  });

  it("is null when the curve is not a length-2 array of pairs", () => {
    expect(
      extractSeries("nx", NAME, { sequence_nx_curve: "not-a-curve", total_bases: 1 }),
    ).toBeNull();
  });
});

describe("busco series", () => {
  it("reads the four completeness percentages", () => {
    const s = extractSeries("busco", NAME, {
      assembly_completeness_single_pct: 90,
      assembly_completeness_duplicated_pct: 4,
      assembly_completeness_fragmented_pct: 3,
      assembly_completeness_missing_pct: 3,
    });
    expect(s).toEqual({
      chartId: "busco",
      name: NAME,
      singlePct: 90,
      duplicatedPct: 4,
      fragmentedPct: 3,
      missingPct: 3,
    });
  });

  it("is null when any of the four is missing", () => {
    expect(
      extractSeries("busco", NAME, {
        assembly_completeness_single_pct: 90,
        assembly_completeness_duplicated_pct: 4,
        assembly_completeness_fragmented_pct: 3,
      }),
    ).toBeNull();
  });
});

describe("qc series", () => {
  it("reads quality_per_position as {position, mean} points", () => {
    const s = extractSeries("qc", NAME, {
      quality_per_position: [
        { position: 1, mean: 38, count: 100 },
        { position: 2, mean: 39, count: 100 },
      ],
    });
    expect(s).toEqual({
      chartId: "qc",
      name: NAME,
      curve: [
        { position: 1, mean: 38, count: 100 },
        { position: 2, mean: 39, count: 100 },
      ],
    });
  });

  it("is null when absent or empty", () => {
    expect(extractSeries("qc", NAME, {})).toBeNull();
    expect(extractSeries("qc", NAME, { quality_per_position: [] })).toBeNull();
  });

  it("is null when a point lacks a numeric mean", () => {
    expect(
      extractSeries("qc", NAME, { quality_per_position: [{ position: 1 }] }),
    ).toBeNull();
  });
});

describe("depth series", () => {
  it("reads the histogram buckets and its own bucket width", () => {
    const s = extractSeries("depth", NAME, {
      bam_stats_depth_histogram: [
        { depth: 0, count: 100 },
        { depth: 10, count: 50 },
      ],
      bam_stats_depth_bucket_width: 10,
    });
    expect(s).toEqual({
      chartId: "depth",
      name: NAME,
      buckets: [
        { depth: 0, count: 100 },
        { depth: 10, count: 50 },
      ],
      bucketWidth: 10,
    });
  });

  it("is null without the bucket width (a shape issue, not just absence)", () => {
    expect(
      extractSeries("depth", NAME, {
        bam_stats_depth_histogram: [{ depth: 0, count: 1 }],
      }),
    ).toBeNull();
  });

  it("is null when absent or empty", () => {
    expect(extractSeries("depth", NAME, {})).toBeNull();
    expect(
      extractSeries("depth", NAME, {
        bam_stats_depth_histogram: [],
        bam_stats_depth_bucket_width: 10,
      }),
    ).toBeNull();
  });
});

describe("unknown chart", () => {
  it("is null for a chart id with no extractor", () => {
    expect(extractSeries("not-a-chart", NAME, {})).toBeNull();
  });
});
