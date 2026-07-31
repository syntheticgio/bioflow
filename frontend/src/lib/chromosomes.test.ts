import { describe, expect, it } from "vitest";
import { classifyChromosomes, isNcbiNucleotideAccession } from "./chromosomes";

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

  // cds_from_genomic.fna, as it really is: the backend parser
  // (backend/app/storage/parsers.py, MAX_STORED_CONTIGS = 50) stores only the
  // first 50 of the file's 8,769 coding records in sequence_lengths. The true
  // count survives separately in sequence_count -- the fixture must match
  // that truncated shape, or it never exercises the real parser output.
  it("rejects a CDS file as not chromosomal", () => {
    const view = classifyChromosomes({
      sequence_count: 8769,
      sequence_names: ["lcl|NC_008409.1_cds_XP_001218755.1_1"],
      sequence_lengths: lengths(
        50,
        1400,
        (i) => `lcl|NC_008409.1_cds_XP_${i}_1`,
      ),
    });
    expect(view.kind).toBe("not-chromosomal");
    if (view.kind === "not-chromosomal") {
      expect(view.reason).toContain("8,769");
    }
  });

  // protein.faa: 8,758 XP_ protein accessions, but only the first 50 are
  // ever stored in sequence_lengths (see MAX_STORED_CONTIGS above).
  it("rejects a protein file as not chromosomal", () => {
    const view = classifyChromosomes({
      sequence_count: 8758,
      sequence_names: ["XP_001218755.1"],
      sequence_lengths: lengths(50, 450, (i) => `XP_00121${i}.1`),
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

  /** The real GCF_000146045.2_R64 yeast genome: 17 sequences, 16 nuclear
   *  chromosomes plus the 85 kb mitochondrion. */
  const YEAST_LENGTHS: Record<string, number> = {
    "NC_001133.9": 230218,
    "NC_001134.8": 813184,
    "NC_001135.5": 316620,
    "NC_001136.10": 1531933,
    "NC_001137.3": 576874,
    "NC_001138.5": 270161,
    "NC_001139.9": 1090940,
    "NC_001140.6": 562643,
    "NC_001141.2": 439888,
    "NC_001142.9": 745751,
    "NC_001143.9": 666816,
    "NC_001144.5": 1078177,
    "NC_001145.3": 924431,
    "NC_001146.8": 784333,
    "NC_001147.6": 1091291,
    "NC_001148.4": 948066,
    "NC_001224.1": 85779,
  };

  it("ranks bars longest first", () => {
    const view = classifyChromosomes({
      sequence_count: 17,
      sequence_names: Object.keys(YEAST_LENGTHS),
      sequence_lengths: YEAST_LENGTHS,
    });
    expect(view.kind).toBe("drawable");
    if (view.kind !== "drawable") return;
    expect(view.bars[0]).toEqual({ name: "NC_001136.10", length: 1531933 });
    expect(view.bars).toHaveLength(17);
    expect(view.overflow).toHaveLength(0);
  });

  // The 100 kb rule decides whether to draw at all; it must never drop a
  // sequence from a file that passed. Yeast's mitochondrion is 85 kb and
  // still belongs on the strip.
  it("keeps sub-100kb sequences as bars once the file qualifies", () => {
    const view = classifyChromosomes({
      sequence_names: Object.keys(YEAST_LENGTHS),
      sequence_lengths: YEAST_LENGTHS,
    });
    if (view.kind !== "drawable") throw new Error("expected drawable");
    expect(view.bars.map((b) => b.name)).toContain("NC_001224.1");
  });

  // A human-like assembly: 24 primary chromosomes plus 200 unplaced
  // scaffolds. The bars must be the 24 biggest, with the rest reachable
  // rather than discarded.
  it("caps bars at 24 and puts the rest in overflow", () => {
    const many: Record<string, number> = {};
    for (let i = 0; i < 24; i++) many[`NC_0000${i}.1`] = 50_000_000 - i * 1000;
    for (let i = 0; i < 200; i++) many[`NW_0001${i}.1`] = 120_000;

    const view = classifyChromosomes({
      sequence_names: Object.keys(many),
      sequence_lengths: many,
    });
    if (view.kind !== "drawable") throw new Error("expected drawable");
    expect(view.bars).toHaveLength(24);
    expect(view.overflow).toHaveLength(200);
    expect(view.bars.every((b) => b.name.startsWith("NC_"))).toBe(true);
  });

  it("marks NCBI RefSeq accessions as linkable", () => {
    const view = classifyChromosomes({
      sequence_names: Object.keys(YEAST_LENGTHS),
      sequence_lengths: YEAST_LENGTHS,
    });
    if (view.kind !== "drawable") throw new Error("expected drawable");
    expect(view.linkable).toBe(true);
  });

  // "One resolvable name is enough" (see the `linkable` comment): a genome
  // can carry an unplaced local-named scaffold alongside real accessions
  // without losing linkability. Without this, mutating the regex to also
  // accept protein accessions would leave the protein-file test passing
  // (it only checks not-chromosomal) while this behavior went untested.
  it("stays linkable when only some bars are NCBI accessions", () => {
    const mixed: Record<string, number> = {
      ...YEAST_LENGTHS,
      local_scaffold_1: 150_000,
    };
    const view = classifyChromosomes({
      sequence_names: Object.keys(mixed),
      sequence_lengths: mixed,
    });
    if (view.kind !== "drawable") throw new Error("expected drawable");
    expect(view.linkable).toBe(true);
  });

  // The local-assembly path. Nothing in the live database exercises this, so
  // it is the branch most likely to break unnoticed.
  it("draws a local assembly but marks it unlinkable", () => {
    const local: Record<string, number> = {};
    for (let i = 1; i <= 8; i++) local[`contig_${i}`] = 900_000 - i * 1000;

    const view = classifyChromosomes({
      sequence_names: Object.keys(local),
      sequence_lengths: local,
    });
    expect(view.kind).toBe("drawable");
    if (view.kind !== "drawable") return;
    expect(view.linkable).toBe(false);
    expect(view.bars).toHaveLength(8);
  });

  // `lcl|NC_008409.1_cds_...` embeds a real accession. An unanchored test
  // would call it linkable and feed NCBI an id it cannot resolve.
  it("does not treat an embedded accession as linkable", () => {
    expect(isNcbiNucleotideAccession("lcl|NC_008409.1_cds_XP_846376.1_2")).toBe(
      false,
    );
    expect(isNcbiNucleotideAccession("NC_001133.9")).toBe(true);
    expect(isNcbiNucleotideAccession("BK006935.2")).toBe(true);
    // A protein accession is resolvable at NCBI but is not a chromosome.
    expect(isNcbiNucleotideAccession("XP_001218755.1")).toBe(false);
  });

  it("labels bars from sequence_labels", () => {
    const view = classifyChromosomes({
      sequence_names: Object.keys(YEAST_LENGTHS),
      sequence_lengths: YEAST_LENGTHS,
      sequence_labels: { "NC_001136.10": "IV", "NC_001224.1": "MT" },
    });
    if (view.kind !== "drawable") throw new Error("expected drawable");
    expect(view.bars[0].label).toBe("IV");
    expect(view.bars.find((b) => b.name === "NC_001224.1")?.label).toBe("MT");
  });

  it("leaves bars unlabelled when a name has no entry", () => {
    const view = classifyChromosomes({
      sequence_names: Object.keys(YEAST_LENGTHS),
      sequence_lengths: YEAST_LENGTHS,
      sequence_labels: { "NC_001136.10": "IV" },
    });
    if (view.kind !== "drawable") throw new Error("expected drawable");
    expect(view.bars.find((b) => b.name === "NC_001133.9")?.label).toBeUndefined();
  });

  // Existing references have no labels at all and must be untouched.
  it("is unchanged when sequence_labels is absent", () => {
    const view = classifyChromosomes({
      sequence_names: Object.keys(YEAST_LENGTHS),
      sequence_lengths: YEAST_LENGTHS,
    });
    if (view.kind !== "drawable") throw new Error("expected drawable");
    expect(view.bars).toHaveLength(17);
    expect(view.bars.every((b) => b.label === undefined)).toBe(true);
  });

  // Labels are cosmetic: a garbage value must not change classification.
  it("ignores a wrong-typed sequence_labels", () => {
    const view = classifyChromosomes({
      sequence_names: Object.keys(YEAST_LENGTHS),
      sequence_lengths: YEAST_LENGTHS,
      sequence_labels: "not an object",
    });
    expect(view.kind).toBe("drawable");
  });
});
