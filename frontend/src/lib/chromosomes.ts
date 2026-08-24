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
  /** Whether NCBI calls this an assembled molecule -- a chromosome or an
   *  organelle of the assembly proper, rather than a scaffold, patch or alt
   *  locus. Absent when the assembly was never looked up. */
  core?: boolean;
  /** The assembly's own ordering, so bars can read 1..22, X, Y, MT rather
   *  than longest-first. Absent alongside `core`. */
  order?: number;
}

/** One sequence's entry in `facts.sequence_roles`, as the backend writes it. */
interface SequenceRole {
  label: string;
  core: boolean;
  order: number;
}

/**
 * Read `facts.sequence_roles`, tolerating everything a fact can turn out to be.
 *
 * Facts are whatever ingest happened to write, across several generations of
 * the schema, so every field is re-checked rather than trusted.
 */
function readRoles(value: unknown): Record<string, SequenceRole> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const out: Record<string, SequenceRole> = {};
  for (const [name, entry] of Object.entries(value as Record<string, unknown>)) {
    if (!entry || typeof entry !== "object" || Array.isArray(entry)) continue;
    const e = entry as Record<string, unknown>;
    if (typeof e.label !== "string" || typeof e.core !== "boolean") continue;
    out[name] = {
      label: e.label,
      core: e.core,
      order: typeof e.order === "number" ? e.order : 0,
    };
  }
  return out;
}

/** Below this, a sequence is not a chromosome or a large scaffold. */
const CHROMOSOME_SCALE_BP = 100_000;

/** Fewer chromosome-scale sequences than this and the file needs the
 *  whole-file check below before it can be called a genome. */
const MIN_CHROMOSOME_SCALE = 5;

/** The parser stores at most this many entries (MAX_STORED_CONTIGS in
 *  backend/app/storage/parsers.py). Equal counts therefore mean every record
 *  in the file is present, and a smaller stored count means a truncated
 *  window over something much larger. */
const MAX_STORED_CONTIGS = 50;

/** A file whose records are all shorter than this is not a genome, however few
 *  of them there are. Sits above a long CDS (~7.7 kb in this data) and below
 *  the smallest genome worth drawing -- HIV-1 at 9.7 kb. */
const SMALL_GENOME_MIN_BP = 8_000;

/** Bars drawn before the rest move to the overflow picker, when nothing tells
 *  us which sequences are chromosomes. Chosen so a human assembly shows its 24
 *  primary chromosomes and yeast shows all 17. */
const MAX_BARS = 24;

/** The same cut when NCBI *did* tell us. Higher because the set is already the
 *  assembly's own chromosomes rather than a length-ranked guess: this is a
 *  guard against a pathological report, not a design limit. The largest real
 *  count observed is 26 (zebrafish). */
const MAX_BARS_WITH_ROLES = 64;

/** An accession body without a RefSeq prefix: either INSDC's two-letters-plus-
 *  six-digits (`CP012345`, `BK006935`) or a WGS contig's four letters plus at
 *  least eight digits (`AAAB01000001`). */
const ACCESSION_BODY = String.raw`(?:[A-Z]{2}\d{6}|[A-Z]{4}\d{8,})`;

/**
 * Whether a sequence name is an accession NCBI's Sequence Viewer can resolve
 * as a nucleotide record.
 *
 * Anchored deliberately: `lcl|NC_008409.1_cds_XP_846376.1_2` contains a real
 * accession but is a local identifier NCBI cannot resolve, and an unanchored
 * match would hand the viewer an id it rejects. `XP_`/`NP_` are excluded on
 * purpose -- they resolve, but as proteins, which is not what a chromosome
 * bar claims to be.
 *
 * The genomic RefSeq prefixes take two different bodies, and assuming they all
 * took digits is what previously excluded every bacterial assembly: `NC_` and
 * friends number their own records (`NC_001133.9`), but `NZ_` wraps an
 * underlying INSDC or WGS accession and keeps its letters
 * (`NZ_CP012345.1`, `NZ_AAAB01000001.1` -- both verified against esummary).
 * Since a wrapped body is exactly what the bare forms below already describe,
 * `NZ_` accepts either shape rather than getting a pattern of its own.
 */
const NUCLEOTIDE_ACCESSION = new RegExp(
  String.raw`^(?:(?:NC|NT|NW|AC)_\d+\.\d+` +
    String.raw`|NZ_(?:\d+|${ACCESSION_BODY})\.\d+` +
    String.raw`|${ACCESSION_BODY}\.\d+)$`,
);

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
 *
 * Commas are the one character kept meaningful rather than stripped: `%ALT`
 * from `bcftools query` (backend/app/pipelines/vcf_stats_runner.py) emits a
 * comma-separated list at a multi-allelic site, e.g. `A,T`. Collapsing that
 * comma would turn a biallelic choice between A and T into what reads as a
 * two-base insertion -- naming a different variant, not just a shorter
 * label. Commas are preserved as `/`, which is not the `mk` field separator
 * and survives `encodeURIComponent` at the call site.
 */
export function markerLabel(ref: string, alt: string): string {
  const clean = (allele: string) =>
    allele
      .split(",")
      .map((a) => a.replace(/[^A-Za-z0-9]/g, ""))
      .filter(Boolean)
      .join("/")
      .slice(0, LABEL_ALLELE_MAX);
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
  const roles = readRoles(facts.sequence_roles);

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
    const role = roles[name];
    // sequence_roles carries its own label and supersedes sequence_labels,
    // which is derived from it. A reference ingested before roles existed has
    // only the latter.
    const label = role ? role.label : labels[name];
    return {
      name,
      length: Number(length) || 0,
      ...(typeof label === "string" && label ? { label } : {}),
      ...(role ? { core: role.core, order: role.order } : {}),
    };
  });
  const trueCount =
    typeof facts.sequence_count === "number"
      ? facts.sequence_count
      : entries.length;
  const bigEnough = entries.filter((e) => e.length >= CHROMOSOME_SCALE_BP);
  const longest = entries.reduce((m, e) => Math.max(m, e.length), 0);

  // Three ways to be a genome, because counting chromosome-scale sequences
  // only recognises the eukaryotes. References here run from viruses to
  // plants, and the first rule alone hid the Sequence Viewer from everything
  // prokaryotic or smaller -- a bacterium has one chromosome, so it could
  // never reach five.
  const isChromosomeSet = bigEnough.length >= MIN_CHROMOSOME_SCALE;
  // A single chromosome-scale sequence that is megabases long: a bacterium.
  const isLoneChromosome = longest >= CHROMOSOME_SCALE_BP;
  // Nothing chromosome-scale, but the file is small, complete, and its
  // records are far longer than coding sequences -- a viral or organellar
  // genome. `trueCount === entries.length` is what makes this safe: a CDS or
  // protein file is truncated at MAX_STORED_CONTIGS, so its true count always
  // exceeds what is stored and it cannot take this branch.
  const isSmallCompleteGenome =
    trueCount === entries.length &&
    entries.length <= MAX_STORED_CONTIGS &&
    longest >= SMALL_GENOME_MIN_BP;

  if (!isChromosomeSet && !isLoneChromosome && !isSmallCompleteGenome) {
    return {
      kind: "not-chromosomal",
      reason: describeNonChromosomal(entries, trueCount),
    };
  }

  // Ranked by length, not file order: chromosome numbers cannot be recovered
  // from an accession like NC_001133.9 without an NCBI lookup, so length is
  // the only ordering available when the assembly was never looked up.
  const byLength = [...entries].sort((a, b) => b.length - a.length);

  // When NCBI told us which sequences are the assembly's chromosomes, draw
  // exactly those -- the set a textbook figure of the species would show --
  // and list the rest. That is 25 bars for GRCh38's 705 sequences and 22 for
  // wheat's 91,589, with no per-species knowledge and no arbitrary cut.
  //
  // A file whose roles are all non-core is a draft assembly with nothing
  // promoted to chromosome. It falls through to the length ranking below
  // rather than drawing an empty strip.
  const core = entries.filter((e) => e.core);
  if (core.length) {
    // The assembly's own order, so bars read 1..22, X, Y, MT. chr11 is longer
    // than chr10, so length ordering would swap them.
    const byOrder = [...core].sort((a, b) => (a.order ?? 0) - (b.order ?? 0));
    const rest = byLength.filter((e) => !e.core);
    return {
      kind: "drawable",
      bars: byOrder.slice(0, MAX_BARS_WITH_ROLES),
      overflow: [...byOrder.slice(MAX_BARS_WITH_ROLES), ...rest],
      linkable: entries.some((b) => isNcbiNucleotideAccession(b.name)),
    };
  }

  const ranked = byLength;

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
