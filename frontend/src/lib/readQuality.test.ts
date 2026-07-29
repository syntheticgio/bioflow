import { describe, expect, it } from "vitest";
import { readQuality } from "./readQuality";
import type { DataObject } from "../api/types";

/** A ready FASTQ with the given facts/metadata. Only the fields readQuality
 *  reads are set; the rest satisfy the type. */
function fastq(
  facts: Record<string, unknown>,
  metadata: Record<string, unknown> = {},
): DataObject {
  return {
    id: "1",
    project_id: "p",
    name: "reads_1.fastq",
    size: 2_100_000_000,
    status: "ready",
    blob_sha256: "abc",
    format: { kind: "fastq" },
    facts,
    metadata,
    tags: [],
    role: null,
    derived_from: [],
    produced_by_job: null,
    mate_object_id: null,
    sidecar_of: null,
    sidecar_role: null,
  } as unknown as DataObject;
}

/** The real facts from DRR1066343_1.fastq, trimmed to what scoring reads. */
const EXAMPLE_FACTS = {
  mean_quality: 38.0,
  min_position_quality: 30.54,
  gc_content_percent: 30.93,
  base_composition: [
    { base: "A", count: 10384579, percent: 34.615 },
    { base: "C", count: 4630603, percent: 15.435 },
    { base: "G", count: 4648787, percent: 15.496 },
    { base: "T", count: 10335189, percent: 34.451 },
    { base: "N", count: 842, percent: 0.003 },
  ],
  qc_before_filtering: { q30_rate: 0.92134, q20_rate: 0.969812 },
  qc_duplication_rate: 0.652221,
};

describe("readQuality", () => {
  it("scores the example file Good (4/5): Excellent Q30, demoted for duplication", () => {
    const q = readQuality(fastq(EXAMPLE_FACTS));
    expect(q).not.toBeNull();
    expect(q!.tier).toBe(4);
    expect(q!.word).toBe("Good");
    expect(q!.basis).toBe("Q30 92.1%");
    expect(q!.caveats.join(" ")).toContain("65% duplication");
  });

  it("does not demote for duplication when the assay expects it", () => {
    const q = readQuality(fastq(EXAMPLE_FACTS, { assay: "RNA-seq" }));
    expect(q!.tier).toBe(5);
    expect(q!.word).toBe("Excellent");
    expect(q!.caveats).toEqual([]);
  });

  it("demotes for duplication when the assay is WGS", () => {
    const q = readQuality(fastq(EXAMPLE_FACTS, { assay: "WGS" }));
    expect(q!.tier).toBe(4);
  });

  it("falls back to mean_quality when fastp has not run", () => {
    const q = readQuality(
      fastq({ mean_quality: 38.0, min_position_quality: 30.54 }),
    );
    expect(q!.tier).toBe(5);
    expect(q!.basis).toBe("mean Q38.0");
  });

  it("demotes a clean average that hides a collapsed tail", () => {
    const q = readQuality(
      fastq({ mean_quality: 38.0, min_position_quality: 12.0 }),
    );
    expect(q!.tier).toBe(4);
    expect(q!.caveats.join(" ")).toContain("drops to Q12");
  });

  it("demotes for a high N rate", () => {
    const q = readQuality(
      fastq({
        mean_quality: 38.0,
        min_position_quality: 30.0,
        base_composition: [{ base: "N", count: 100, percent: 5.0 }],
      }),
    );
    expect(q!.tier).toBe(4);
    expect(q!.caveats.join(" ")).toContain("5% ambiguous");
  });

  it("floors at 1 rather than going to zero", () => {
    const q = readQuality(
      fastq({
        qc_before_filtering: { q30_rate: 0.4 },
        mean_quality: 15,
        min_position_quality: 5,
        base_composition: [{ base: "N", count: 100, percent: 5.0 }],
      }),
    );
    expect(q!.tier).toBe(1);
    expect(q!.word).toBe("Unsuitable");
  });

  it("never reports GC as a caveat", () => {
    const q = readQuality(fastq(EXAMPLE_FACTS));
    expect(q!.caveats.join(" ").toLowerCase()).not.toContain("gc");
  });

  it("assembles a tooltip with the word, score and basis", () => {
    const q = readQuality(fastq(EXAMPLE_FACTS));
    expect(q!.tooltip).toContain("Good (4/5)");
    expect(q!.tooltip).toContain("Q30 92.1%");
    expect(q!.tooltip).toContain("Assay");
  });

  it("omits the assay hint once the assay is known", () => {
    const q = readQuality(fastq(EXAMPLE_FACTS, { assay: "WGS" }));
    expect(q!.tooltip).not.toContain("Set Assay");
  });

  it("returns null for the sixth state", () => {
    // Not a read file.
    const bam = { ...fastq(EXAMPLE_FACTS), format: { kind: "bam" } };
    expect(readQuality(bam as unknown as DataObject)).toBeNull();
    // A FASTQ with no quality facts yet.
    expect(readQuality(fastq({}))).toBeNull();
    // Still ingesting.
    const pending = { ...fastq(EXAMPLE_FACTS), status: "ingesting" };
    expect(readQuality(pending as unknown as DataObject)).toBeNull();
  });
});
