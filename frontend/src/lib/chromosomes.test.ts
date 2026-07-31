import { describe, expect, it } from "vitest";
import {
  classifyChromosomes,
  focusWindow,
  isNcbiNucleotideAccession,
  markerLabel,
} from "./chromosomes";

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

  // Previously rejected for having only one chromosome-scale sequence. That
  // rule could not tell this from a bacterium, which is exactly the shape it
  // was excluding, so a lone 500 kb sequence now draws.
  it("draws a genome with one chromosome-scale sequence plus small contigs", () => {
    const view = classifyChromosomes({
      sequence_count: 3,
      sequence_names: ["NC_000001.1", "NC_000002.1", "NC_000003.1"],
      sequence_lengths: {
        "NC_000001.1": 500_000,
        "NC_000002.1": 4_000,
        "NC_000003.1": 3_000,
      },
    });
    expect(view.kind).toBe("drawable");
  });

  // The real GCF_002310435.1: S. aureus Newman, one 2.9 Mb chromosome. A
  // bacterium can never reach five chromosome-scale sequences, so the old
  // count rule hid the Sequence Viewer from every prokaryote.
  it("draws a single-chromosome bacterial genome", () => {
    const view = classifyChromosomes({
      sequence_count: 1,
      sequence_names: ["NZ_CP023390.1"],
      sequence_lengths: { "NZ_CP023390.1": 2_878_897 },
    });
    expect(view.kind).toBe("drawable");
    if (view.kind === "drawable") {
      expect(view.bars.map((b) => b.name)).toEqual(["NZ_CP023390.1"]);
      expect(view.linkable).toBe(true);
    }
  });

  // Nothing chromosome-scale at all, but complete and far longer than coding
  // records. References here run from viruses to plants.
  it("draws a small complete viral genome", () => {
    const view = classifyChromosomes({
      sequence_count: 1,
      sequence_names: ["NC_045512.2"],
      sequence_lengths: { "NC_045512.2": 29_903 },
    });
    expect(view.kind).toBe("drawable");
  });

  // A segmented viral genome: eight records, all short, but all of them.
  it("draws a segmented viral genome", () => {
    const view = classifyChromosomes({
      sequence_count: 8,
      sequence_names: ["NC_002023.1"],
      sequence_lengths: lengths(8, 2_300, (i) => `NC_00202${i}.1`),
    });
    // Every segment is under the 8 kb floor, so this stays rejected -- the
    // floor is what keeps a handful of CDS records out, and influenza's
    // segments are genuinely CDS-sized. Documented rather than fixed: the
    // Sequence Viewer is still reachable per-sequence from a variant row.
    expect(view.kind).toBe("not-chromosomal");
  });

  // The floor's real job: a small file of short records is not a genome even
  // though nothing is truncated.
  it("rejects a short complete file below the small-genome floor", () => {
    const view = classifyChromosomes({
      sequence_count: 6,
      sequence_names: ["XP_001218755.1"],
      sequence_lengths: lengths(6, 820, (i) => `XP_00121${i}.1`),
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

  // `NZ_` wraps an underlying INSDC or WGS accession and keeps its letters,
  // unlike `NC_`, which numbers its own records. Assuming digits everywhere
  // excluded every bacterial RefSeq assembly. All four verified resolvable
  // through NCBI's esummary.
  it("accepts NZ_ accessions, whose bodies carry letters", () => {
    // Complete bacterial genomes: two letters plus six digits.
    expect(isNcbiNucleotideAccession("NZ_CP012345.1")).toBe(true);
    expect(isNcbiNucleotideAccession("NZ_LR134386.1")).toBe(true);
    // A WGS contig: four letters plus eight or more digits.
    expect(isNcbiNucleotideAccession("NZ_AAAB01000001.1")).toBe(true);
    // The all-digit form stays valid -- this widened the rule, not moved it.
    expect(isNcbiNucleotideAccession("NZ_123456.1")).toBe(true);
  });

  // Widening NZ_ must not widen the prefixes that really are all digits, or
  // the viewer gets handed ids NCBI rejects.
  it("still rejects lettered bodies under digit-only prefixes", () => {
    expect(isNcbiNucleotideAccession("NC_CP012345.1")).toBe(false);
    expect(isNcbiNucleotideAccession("NW_AAAB01000001.1")).toBe(false);
    // A version suffix is still required.
    expect(isNcbiNucleotideAccession("NZ_CP012345")).toBe(false);
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

describe("focusWindow", () => {
  // A 10 kb virus: 1% is 100 bases, so the 2 kb floor takes over -- a 4 kb
  // span rather than a 200 b one that would show nothing around the variant.
  it("applies the floor on a small viral genome", () => {
    expect(focusWindow(5_000, 10_000)).toEqual([3_000, 7_000]);
  });

  // Smaller than one full window: clamping at both ends yields the whole
  // sequence, which is the right answer for a tiny genome.
  it("shows the whole sequence when it is shorter than the window", () => {
    expect(focusWindow(1_500, 3_000)).toEqual([1, 3_000]);
  });

  // A 250 Mb plant chromosome: 1% is 2.5 Mb, so the 200 kb ceiling applies
  // and the view stays readable instead of becoming a smear.
  it("applies the ceiling on a large chromosome", () => {
    expect(focusWindow(100_000_000, 250_000_000)).toEqual([
      99_800_000, 100_200_000,
    ]);
  });

  // A 5 Mb bacterial genome: 1% is 50 kb, between floor and ceiling.
  it("uses one percent of length between the bounds", () => {
    expect(focusWindow(2_500_000, 5_000_000)).toEqual([2_450_000, 2_550_000]);
  });

  it("clamps to the start of the sequence", () => {
    expect(focusWindow(100, 5_000_000)).toEqual([1, 50_100]);
  });

  it("clamps to the end of the sequence", () => {
    expect(focusWindow(4_999_900, 5_000_000)).toEqual([4_949_900, 5_000_000]);
  });
});

describe("markerLabel", () => {
  it("formats a simple SNV", () => {
    expect(markerLabel("G", "A")).toBe("G-to-A");
  });

  // `|` separates fields inside NCBI's mk parameter, so an allele containing
  // one would silently corrupt the marker spec.
  it("strips characters that would break the mk parameter", () => {
    expect(markerLabel("<DEL>", "A|B")).toBe("DEL-to-AB");
  });

  // Indel alleles run to kilobases; the marker label is not where that belongs.
  it("truncates long indel alleles", () => {
    expect(markerLabel("A".repeat(40), "T")).toBe(`${"A".repeat(12)}-to-T`);
  });

  it("falls back when sanitising empties both alleles", () => {
    expect(markerLabel("|", "*")).toBe("variant");
  });

  // %ALT emits a comma-separated list at a multi-allelic site. Collapsing the
  // comma would turn a biallelic SNV into a two-base insertion -- a different
  // variant, not a shorter label.
  it("keeps alternate alleles distinct", () => {
    expect(markerLabel("G", "A,T")).toBe("G-to-A/T");
  });
});
