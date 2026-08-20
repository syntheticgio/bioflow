import { describe, expect, it } from "vitest";

import { countVisibleFacts, FactsTable } from "./FactsTable";

describe("FactsTable suppression", () => {
  it("suppresses qc_*, gc_bias_*, and gc_blob_* keys in countVisibleFacts", () => {
    const facts = {
      read_count: 1000,
      paired: true,
      // QC facts (suppressed)
      qc_adapter_content: "pass",
      qc_per_base_sequence_quality: "pass",
      // GC bias facts (suppressed)
      gc_bias_status: "ok",
      gc_bias_curve: [
        { gc_min: 0, gc_max: 5, mean_depth: 10.2, window_count: 50 },
      ],
      gc_bias_partial: false,
      gc_bias_computed_at: "2026-08-20T00:00:00Z",
      // GC blob facts (suppressed)
      gc_blob_status: "ok",
      gc_blob_report: "gc_blob.json",
      gc_blob_contig_count: 12,
      gc_blob_dropped_count: 0,
      // BAM stats suppressed keys
      bam_stats_summary: { mapped: 1000 },
      bam_stats_coverage_bins: [1, 2, 3],
    };

    // Only read_count and paired should be visible (2 visible facts)
    expect(countVisibleFacts(facts)).toBe(2);
  });

  it("returns null when all facts are suppressed and no parse errors exist", () => {
    const gcOnlyFacts = {
      gc_bias_status: "ok",
      gc_bias_curve: [],
      gc_blob_status: "ok",
      gc_blob_report: "gc_blob.json",
    };

    const rendered = FactsTable({ facts: gcOnlyFacts });
    expect(rendered).toBeNull();
  });
});
