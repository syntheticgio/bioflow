/**
 * What a reference FASTA's sequences can be shown as.
 *
 * A reference is not automatically a set of chromosomes: the same project can
 * hold a 17-sequence genome, a `cds_from_genomic.fna` with 8,769 coding
 * records, and a `protein.faa`. Drawing chromosome bars for the latter two
 * would be the same category error the Actions-tab suggestion rules once made
 * by treating every FASTA as an alignable reference.
 *
 * A tagged union rather than a nullable result so the caller renders per case
 * and cannot silently drop one.
 */
export type ChromosomeView =
  | { kind: "drawable"; bars: Bar[]; overflow: Bar[]; linkable: boolean }
  /** Names parsed, lengths never measured -- an object ingested before
   *  `sequence_lengths` was added. Re-running QC populates it. */
  | { kind: "needs-qc" }
  | { kind: "not-chromosomal"; reason: string }
  | { kind: "nothing" };

export interface Bar {
  name: string;
  length: number;
}

/** Below this, a sequence is not a chromosome or a large scaffold. */
const CHROMOSOME_SCALE_BP = 100_000;

/** Fewer chromosome-scale sequences than this and the file is something else
 *  -- coding sequences, proteins, a lone plasmid. */
const MIN_CHROMOSOME_SCALE = 5;

/** Bars drawn before the rest move to the overflow picker. Chosen so a human
 *  assembly shows its 24 primary chromosomes and yeast shows all 17. */
const MAX_BARS = 24;

export function classifyChromosomes(
  facts: Record<string, unknown>,
): ChromosomeView {
  const names = Array.isArray(facts.sequence_names)
    ? (facts.sequence_names as string[])
    : [];
  const lengths =
    facts.sequence_lengths && typeof facts.sequence_lengths === "object"
      ? (facts.sequence_lengths as Record<string, number>)
      : {};
  const lengthCount = Object.keys(lengths).length;

  if (!names.length && !lengthCount) return { kind: "nothing" };
  if (!lengthCount) return { kind: "needs-qc" };

  const entries: Bar[] = Object.entries(lengths).map(([name, length]) => ({
    name,
    length: Number(length) || 0,
  }));
  const bigEnough = entries.filter((e) => e.length >= CHROMOSOME_SCALE_BP);

  if (bigEnough.length < MIN_CHROMOSOME_SCALE) {
    return { kind: "not-chromosomal", reason: describeNonChromosomal(entries) };
  }

  // Ranked by length, not file order: chromosome numbers cannot be recovered
  // from an accession like NC_001133.9 without an NCBI lookup this design
  // does without, and ranking is what makes the top-24 cut meaningful.
  const ranked = [...entries].sort((a, b) => b.length - a.length);

  return {
    kind: "drawable",
    bars: ranked.slice(0, MAX_BARS),
    overflow: ranked.slice(MAX_BARS),
    linkable: false,
  };
}

/**
 * Why this file is not a chromosome set, in terms of what it actually holds.
 *
 * "None over 100 kb" is the useful half of the message: it tells the user the
 * file is short records, without claiming to know whether they are CDS,
 * proteins or something else.
 */
function describeNonChromosomal(entries: Bar[]): string {
  const count = entries.length.toLocaleString();
  const longest = entries.reduce((m, e) => Math.max(m, e.length), 0);
  if (longest < CHROMOSOME_SCALE_BP) {
    return `${count} sequences, none over 100 kb — this looks like coding sequences or proteins, not chromosomes.`;
  }
  return `${count} sequences, too few of them chromosome-scale to draw a chromosome map.`;
}
