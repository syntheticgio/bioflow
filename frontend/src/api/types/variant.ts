/**
 * Variant callers. DeepVariant is recognized but not installed -- there is no
 * arm64 build -- and the server refuses it with an explanation rather than
 * failing obscurely, so the dialog can show it as unavailable.
 */
export type VariantCallerName = "clair3" | "bcftools" | "deepvariant";

/** Mirrors variant_runner.VariantParams. */
export interface VariantParams {
  caller: VariantCallerName;
  threads: number;
}

export interface VariantDefaults {
  params: VariantParams;
  /** Null when the chemistry is CLR: no caller is offered for it. */
  caller: VariantCallerName | null;
  /** The chemistry QC inferred, which is what picked the caller. */
  chemistry: string | null;
  /** Resolved from the BAM's provenance; null for an uploaded BAM. */
  reference_id: string | null;
  reference_name: string | null;
  /** True when the dialog must ask the user to choose a reference. */
  needs_reference: boolean;
  callers: { name: VariantCallerName; available: boolean }[];
  max_threads: number;
}

export interface VariantRequest {
  bam_id: string;
  reference_id?: string | null;
  caller?: VariantCallerName | null;
  params?: Partial<VariantParams>;
  /** Consent to a multi-gigabyte on-demand-tool download. Without it, a
   *  request against a not-yet-installed optional caller (DeepVariant) is
   *  refused with a 422 naming the download size, in `details.download_bytes`
   *  -- re-post with this set once the user has actually agreed to it. */
  install_optional?: boolean;
  resource_override?: boolean;
}

// --- Variant results (vcfstats) ---

/** One bucket of a re-binned distribution. `value` is the bucket's lower
 *  bound, so an axis can be labelled without knowing the bucket width. */
export interface HistogramBucket {
  value: number;
  count: number;
}

export interface VariantSummary {
  variants: number;
  snps: number;
  indels: number;
  multiallelic: number;
  samples: number;
  ts: number;
  tv: number;
  ti_tv: number;
  pass_count: number;
  no_filter_count: number;
  /** Absent when the file does not use FILTER at all -- bcftools call does
   *  not stamp PASS, and reporting a rate for such a file would misstate it. */
  pass_pct?: number;
}

export interface VariantContigRow {
  contig: string;
  length: number;
  variants: number;
  snps: number;
  indels: number;
  per_kb: number;
}

export interface VcfStatsFacts extends Record<string, unknown> {
  vcf_stats_status?: string;
  vcf_stats_tool_version?: string;
  vcf_stats_summary?: VariantSummary;
  vcf_stats_qual_histogram?: HistogramBucket[];
  vcf_stats_depth_histogram?: HistogramBucket[];
  vcf_stats_substitutions?: { type: string; count: number }[];
  vcf_stats_indel_lengths?: { length: number; count: number }[];
  vcf_stats_filters?: { filter: string; count: number }[];
  vcf_stats_density_bins?: number[];
  vcf_stats_density_bounds?: { contig: string; bin_start: number }[];
  vcf_stats_contigs?: VariantContigRow[];
  vcf_stats_report?: string;
  vcf_stats_db?: string;
}

export interface VariantRow {
  chrom: string;
  pos: number;
  ref: string;
  alt: string;
  qual: number | null;
  filter: string;
  dp: number | null;
  gt: string;
  /** Present only on an annotated VCF; null on every row of an un-annotated
   *  one, which is the common case. */
  gene: string | null;
  consequence: string | null;
  aa_change: string | null;
  aa_pos: number | null;
}

/** What one gene resolved to at UniProt.
 *
 *  A null `accession` is an ordinary answer, not an error: the symbol matched
 *  no reviewed protein, every candidate was too short to hold the variant's
 *  residue, or UniProt was unreachable. All three mean the same thing to the
 *  reader, so the server does not distinguish them and neither does the UI.
 *
 *  An empty `pdb_ids` on a non-null accession is different and more common --
 *  the protein is identified and simply has no solved structure, which is the
 *  case for roughly two thirds of resolved genes. */
export interface VariantStructure {
  gene: string;
  accession: string | null;
  pdb_ids: string[];
  /** The resolved protein's length. Shown so a reader can sanity-check that
   *  the residue they came for is actually inside this protein. */
  length: number | null;
}

export interface VariantsPage {
  /** Null when the request set skip_count -- the caller keeps its previous
   *  total, because only the page number changed. */
  total: number | null;
  rows: VariantRow[];
}

export interface VariantQuery {
  offset: number;
  limit: number;
  contig?: string;
  posMin?: number;
  posMax?: number;
  filterValue?: string;
  variantType?: string;
  minQual?: number;
  consequence?: string;
  skipCount?: boolean;
}

// --- Structural variant results (Sniffles2) ---

/** One row of the SV table. Mirrors app.pipelines.sv_db.SvRecord field for
 *  field, including its casing -- the route returns the SQLite row as-is. */
export interface SvRecord {
  chrom: string;
  pos: number;
  /** Null for a breakend, which joins two loci rather than spanning one. */
  end: number | null;
  svtype: string;
  /** A magnitude, never negative -- the sign a deletion's SVLEN carries in
   *  the VCF is redundant with svtype and is stripped by sv_db. Null for a
   *  breakend, which has no length. */
  svlen: number | null;
  qual: number | null;
  filter: string;
  /** Read support for the call. Null when Sniffles2 did not report SUPPORT. */
  support: number | null;
  gt: string;
  /** The paired breakend's ID, for a BND record. Null otherwise. */
  mate: string | null;
}

export interface SvsPage {
  /** Null when the request set skip_count -- the caller keeps its previous
   *  total, matching VariantsPage. */
  total: number | null;
  rows: SvRecord[];
}

export interface SvQuery {
  offset: number;
  limit: number;
  contig?: string;
  posMin?: number;
  posMax?: number;
  svtype?: string;
  minLength?: number;
  maxLength?: number;
  filterValue?: string;
  minQual?: number;
  skipCount?: boolean;
}

/** One log-scaled length bin. `min_length` is the bin's inclusive lower
 *  bound, so an axis can be labelled without knowing the bin width -- the
 *  bins are not equal width, unlike VariantResults' HistogramBucket. */
export interface SvLengthBucket {
  label: string;
  min_length: number;
  count: number;
}

export interface SvSummary {
  type_counts: Record<string, number>;
  length_histogram: SvLengthBucket[];
}
