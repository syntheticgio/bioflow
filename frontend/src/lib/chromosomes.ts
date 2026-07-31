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
  /** NCBI's name for this sequence ("IV", "MT", "chr11-scaffold01"), when the
   *  assembly lookup found one. Absent on references ingested before labels
   *  were fetched, and on any locally-assembled file. */
  label?: string;
}

/** Below this, a sequence is not a chromosome or a large scaffold. */
const CHROMOSOME_SCALE_BP = 100_000;

/** Fewer chromosome-scale sequences than this and the file is something else
 *  -- coding sequences, proteins, a lone plasmid. */
const MIN_CHROMOSOME_SCALE = 5;

/** Bars drawn before the rest move to the overflow picker. Chosen so a human
 *  assembly shows its 24 primary chromosomes and yeast shows all 17. */
const MAX_BARS = 24;

/**
 * Whether a sequence name is an accession NCBI's Sequence Viewer can resolve
 * as a nucleotide record.
 *
 * Anchored deliberately: `lcl|NC_008409.1_cds_XP_846376.1_2` contains a real
 * accession but is a local identifier NCBI cannot resolve, and an unanchored
 * match would hand the viewer an id it rejects. `XP_`/`NP_` are excluded on
 * purpose -- they resolve, but as proteins, which is not what a chromosome
 * bar claims to be.
 */
const NUCLEOTIDE_ACCESSION =
  /^(?:(?:NC|NZ|NT|NW|AC)_\d+\.\d+|[A-Z]{2}\d{6}\.\d+|[A-Z]{4}\d{8,}\.\d+)$/;

export function isNcbiNucleotideAccession(name: string): boolean {
  return NUCLEOTIDE_ACCESSION.test(name.trim());
}

/** Fraction of a sequence's length shown to each side of a focused position. */
const FOCUS_FRACTION = 0.01;
/** Smallest half-window, so a short viral genome does not zoom to a near-empty
 *  view. */
const FOCUS_MIN_HALF = 2_000;
/** Largest half-window, so a plant chromosome does not become an unreadable
 *  smear. */
const FOCUS_MAX_HALF = 200_000;

/**
 * The visible range to show around a focused position, as [start, end], 1-based
 * and inclusive.
 *
 * Scaled rather than fixed because references here run from viruses to plants
 * -- four orders of magnitude. A constant flank that frames a gene in a plant
 * genome is the entire genome of a virus, and one that suits a virus crops a
 * plant gene to a fragment.
 *
 * The fraction and both bounds are judgment, not measurement: they are starting
 * points chosen to degrade sensibly at both ends of that range. Tune them if
 * they read wrong in practice.
 */
export function focusWindow(
  position: number,
  sequenceLength: number,
): [number, number] {
  const half = Math.min(
    FOCUS_MAX_HALF,
    Math.max(FOCUS_MIN_HALF, Math.round(sequenceLength * FOCUS_FRACTION)),
  );
  return [
    Math.max(1, position - half),
    Math.min(sequenceLength, position + half),
  ];
}

/** Longest allele fragment kept in a marker label. */
const LABEL_ALLELE_MAX = 12;

/**
 * A marker name for one variant, safe to interpolate into NCBI's `mk`
 * parameter.
 *
 * NCBI warns that special characters in marker names "must be escaped
 * properly", and `|` is the separator between position, name and colour within
 * `mk` -- so an allele carrying one would corrupt the spec rather than just
 * look wrong. VCF also permits symbolic alleles such as `<DEL>` and `*`.
 * Rather than escape a moving target, reduce the label to plain ASCII.
 */
export function markerLabel(ref: string, alt: string): string {
  const clean = (allele: string) =>
    allele.replace(/[^A-Za-z0-9]/g, "").slice(0, LABEL_ALLELE_MAX);
  const r = clean(ref);
  const a = clean(alt);
  if (!r && !a) return "variant";
  return `${r || "?"}-to-${a || "?"}`;
}

export function classifyChromosomes(
  facts: Record<string, unknown>,
): ChromosomeView {
  const names = Array.isArray(facts.sequence_names)
    ? (facts.sequence_names as string[])
    : [];
  const lengths =
    facts.sequence_lengths &&
    typeof facts.sequence_lengths === "object" &&
    !Array.isArray(facts.sequence_lengths)
      ? (facts.sequence_lengths as Record<string, number>)
      : {};
  const lengthCount = Object.keys(lengths).length;
  const labels =
    facts.sequence_labels &&
    typeof facts.sequence_labels === "object" &&
    !Array.isArray(facts.sequence_labels)
      ? (facts.sequence_labels as Record<string, string>)
      : {};

  if (!names.length && !lengthCount) return { kind: "nothing" };
  if (!lengthCount) return { kind: "needs-qc" };

  // The parser (backend/app/storage/parsers.py) caps stored entries at
  // MAX_STORED_CONTIGS = 50, so `entries` may be a truncated window over the
  // file rather than the whole thing -- true count lives in
  // facts.sequence_count. The >=5-chromosome-scale check below and the
  // overflow list further down both operate on this possibly-truncated
  // window: for a real file with >50 records, "too few chromosome-scale
  // sequences" or a short overflow list may reflect what got stored, not
  // what the file actually contains.
  const entries: Bar[] = Object.entries(lengths).map(([name, length]) => {
    const label = labels[name];
    return {
      name,
      length: Number(length) || 0,
      ...(typeof label === "string" && label ? { label } : {}),
    };
  });
  const trueCount =
    typeof facts.sequence_count === "number"
      ? facts.sequence_count
      : entries.length;
  const bigEnough = entries.filter((e) => e.length >= CHROMOSOME_SCALE_BP);

  if (bigEnough.length < MIN_CHROMOSOME_SCALE) {
    return {
      kind: "not-chromosomal",
      reason: describeNonChromosomal(entries, trueCount),
    };
  }

  // Ranked by length, not file order: chromosome numbers cannot be recovered
  // from an accession like NC_001133.9 without an NCBI lookup this design
  // does without, and ranking is what makes the top-24 cut meaningful.
  const ranked = [...entries].sort((a, b) => b.length - a.length);

  return {
    kind: "drawable",
    bars: ranked.slice(0, MAX_BARS),
    overflow: ranked.slice(MAX_BARS),
    // One resolvable name is enough: a genome can carry an unplaced scaffold
    // with a local name without that making the chromosomes unlinkable. Each
    // bar is re-checked individually at render time.
    linkable: ranked.some((b) => isNcbiNucleotideAccession(b.name)),
  };
}

/**
 * Why this file is not a chromosome set, in terms of what it actually holds.
 *
 * "None over 100 kb" is the useful half of the message: it tells the user the
 * file is short records, without claiming to know whether they are CDS,
 * proteins or something else.
 */
function describeNonChromosomal(entries: Bar[], trueCount: number): string {
  const count = trueCount.toLocaleString();
  const longest = entries.reduce((m, e) => Math.max(m, e.length), 0);
  if (longest < CHROMOSOME_SCALE_BP) {
    return `${count} sequences, none over 100 kb — this looks like coding sequences or proteins, not chromosomes.`;
  }
  return `${count} sequences, too few of them chromosome-scale to draw a chromosome map.`;
}
