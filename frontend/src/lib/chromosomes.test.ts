import { describe, expect, it } from "vitest";
import { classifyChromosomes } from "./chromosomes";

describe("classifyChromosomes", () => {
  // genomic.gff: parsed, but carries no sequence names at all.
  it("returns nothing when there are no sequence facts", () => {
    expect(classifyChromosomes({}).kind).toBe("nothing");
    expect(classifyChromosomes({ sequence_names: [] }).kind).toBe("nothing");
  });

  // The real GCA_000146045.2_R64 and one of the two GCF_000002445.2 objects:
  // ingested before sequence_lengths existed, so names are known but no
  // lengths are. Re-running QC is what fixes this, so say so.
  it("returns needs-qc when names are known but lengths are not", () => {
    const view = classifyChromosomes({
      sequence_names: ["BK006935.2", "BK006936.2"],
      sequence_count: 16,
    });
    expect(view.kind).toBe("needs-qc");
  });

  it("returns needs-qc when sequence_lengths is present but empty", () => {
    const view = classifyChromosomes({
      sequence_names: ["BK006935.2"],
      sequence_lengths: {},
    });
    expect(view.kind).toBe("needs-qc");
  });

  /** N sequences of `len` bases, named by the given pattern. */
  function lengths(n: number, len: number, name: (i: number) => string) {
    const out: Record<string, number> = {};
    for (let i = 0; i < n; i++) out[name(i)] = len;
    return out;
  }

  // cds_from_genomic.fna, as it really is: 8,769 coding records whose names
  // are `lcl|` local identifiers NCBI cannot resolve.
  it("rejects a CDS file as not chromosomal", () => {
    const view = classifyChromosomes({
      sequence_count: 8769,
      sequence_names: ["lcl|NC_008409.1_cds_XP_001218755.1_1"],
      sequence_lengths: lengths(
        8769,
        1400,
        (i) => `lcl|NC_008409.1_cds_XP_${i}_1`,
      ),
    });
    expect(view.kind).toBe("not-chromosomal");
    if (view.kind === "not-chromosomal") {
      expect(view.reason).toContain("8,769");
    }
  });

  // protein.faa: 8,758 XP_ protein accessions. Real accessions, wrong molecule.
  it("rejects a protein file as not chromosomal", () => {
    const view = classifyChromosomes({
      sequence_count: 8758,
      sequence_names: ["XP_001218755.1"],
      sequence_lengths: lengths(8758, 450, (i) => `XP_00121${i}.1`),
    });
    expect(view.kind).toBe("not-chromosomal");
  });

  // A plasmid-only or single-contig file: real DNA, too few chromosome-scale
  // sequences to be a chromosome set.
  it("rejects a file with too few chromosome-scale sequences", () => {
    const view = classifyChromosomes({
      sequence_count: 3,
      sequence_names: ["NC_000001.1", "NC_000002.1", "NC_000003.1"],
      sequence_lengths: {
        "NC_000001.1": 500_000,
        "NC_000002.1": 4_000,
        "NC_000003.1": 3_000,
      },
    });
    expect(view.kind).toBe("not-chromosomal");
  });
});
