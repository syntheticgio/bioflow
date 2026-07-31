import { describe, expect, it } from "vitest";
import { canShowStructure, isResidueChanging } from "./variants";

/** Every consequence type the real yeast callset actually produced, with its
 *  count, so the cases here are the ones that occur rather than ones invented
 *  to match the implementation. */
describe("isResidueChanging", () => {
  it("accepts the types that alter the protein", () => {
    for (const kind of [
      "missense", // 1,653
      "frameshift", // 58
      "stop_gained", // 32
      "inframe_deletion", // 26
      "inframe_insertion", // 10
      "start_lost", // 5
      "stop_lost", // 2
    ]) {
      expect(isResidueChanging(kind), kind).toBe(true);
    }
  });

  it("rejects synonymous, the largest annotated class", () => {
    // 2,173 of 4,060 annotated variants. They carry an aa_pos but change no
    // residue, so a structure view would show an unchanged protein.
    expect(isResidueChanging("synonymous")).toBe(false);
  });

  it("rejects consequences outside the coding sequence", () => {
    for (const kind of ["intron", "non_coding", "splice_region"]) {
      expect(isResidueChanging(kind), kind).toBe(false);
    }
  });

  it("accepts a compound consequence when any part changes the protein", () => {
    // `stop_lost&frameshift` occurs once in the real callset.
    expect(isResidueChanging("stop_lost&frameshift")).toBe(true);
  });

  it("rejects a compound consequence when no part does", () => {
    expect(isResidueChanging("synonymous&intron")).toBe(false);
  });

  it("rejects an unrecognised type", () => {
    // A missing button is a gap; a button that opens a structure view of
    // something that may not touch the protein is the app asserting a wrong
    // thing. bcftools can emit types this app has never seen.
    expect(isResidueChanging("some_future_consequence")).toBe(false);
  });

  it("rejects an absent consequence, the un-annotated case", () => {
    expect(isResidueChanging(null)).toBe(false);
    expect(isResidueChanging("")).toBe(false);
  });
});

describe("canShowStructure", () => {
  const missense = {
    gene: "PKC1",
    consequence: "missense",
    aa_pos: 866,
  };

  it("accepts a residue-changing row with a gene and a position", () => {
    expect(canShowStructure(missense)).toBe(true);
  });

  it("rejects a row with no gene", () => {
    // Nothing to ask UniProt about.
    expect(canShowStructure({ ...missense, gene: null })).toBe(false);
  });

  it("rejects a row with no residue", () => {
    // The residue is what picks between two proteins sharing a gene symbol,
    // so without it the resolver's length guard has nothing to check.
    expect(canShowStructure({ ...missense, aa_pos: null })).toBe(false);
  });

  it("rejects a synonymous row even though it has a position", () => {
    expect(
      canShowStructure({ ...missense, consequence: "synonymous" }),
    ).toBe(false);
  });

  it("rejects an un-annotated row, the common case", () => {
    expect(
      canShowStructure({ gene: null, consequence: null, aa_pos: null }),
    ).toBe(false);
  });
});
