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
});
