import type { ObjectRole } from "./object";
import type { MemoryModel, ParamFieldMeta } from "./pipeline";

export type AlignerName =
  | "bwa-mem2"
  | "minimap2"
  | "bowtie2"
  | "hisat2"
  | "star";

/** minimap2 presets. The wrong one for long reads aligns poorly rather than failing. */
export type AlignPreset = "map-ont" | "map-pb" | "map-hifi" | "lr:hq" | "sr";

/**
 * Mirrors align_params.BaseAlignParams plus whichever subclass is in play.
 * Tool-specific keys are optional because they only exist for one aligner --
 * the backend rejects a key belonging to a different tool rather than
 * ignoring it, so sending the wrong one fails loudly at launch.
 */
export interface AlignParams {
  aligner: AlignerName;
  threads: number;
  sort_memory_mb: number;
  mark_duplicates: boolean;
  // AlignPreset | "" would be accurate for minimap2/winnowmap alone, whose
  // fixed vocabularies MINIMAP2_PRESETS/WINNOWMAP_PRESETS the backend
  // actually validates against -- but align_params.BaseAlignParams.preset is
  // a bare `str = ""` with no fixed vocabulary, and bwa-mem2's organism
  // presets (AlignerSchema.presets, an open Record<string, AlignerPreset>)
  // write arbitrary ids like "human" into this same field. Widened to match
  // what the backend actually accepts, at the cost of losing autocomplete
  // for the minimap2/winnowmap literals.
  preset?: AlignPreset | string;
  sensitivity?: string;
  local?: boolean;
  maxins?: number;
  no_mixed?: boolean;
  no_discordant?: boolean;
  report_k?: number;
  rna_strandness?: string;
  max_intronlen?: number;
  no_spliced_alignment?: boolean;
  dta?: boolean;
  two_pass?: boolean;
  out_filter_multimap_nmax?: number;
  align_intron_max?: number;
  out_sam_unmapped?: boolean;
  // bwa-mem2 specific fields
  min_score?: number;
  mark_split?: boolean;
  max_seed_occ?: number;
  reseed_factor?: number;
  all_alignments?: boolean;
  max_mate_rescue?: number;
  soft_clip_supp?: boolean;
  clip_penalty?: string;
  multimap_xa?: string;
  batch_size?: number;
  /** Not an aligner option -- carried alongside the rest of `params` on
   *  launch because AlignRequest.params is an open dict, not a typed
   *  sub-model, so this is where the dialog's chunked-alignment toggle
   *  actually travels to the backend. */
  chunked?: boolean;
}

export interface AlignerPreset {
  id: string;
  label: string;
  description: string;
  values: Record<string, unknown>;
}

export interface AlignerSchema {
  aligner: AlignerName;
  fields: ParamFieldMeta[];
  presets?: Record<string, AlignerPreset>;
}

/**
 * Fetched once per dialog open. The client evaluates the same arithmetic the
 * backend does against these coefficients, so sliders give instant feedback
 * without a request per keystroke -- and the backend re-checks at launch.
 */
export interface AlignEnvelope {
  cpu_budget: number | null;
  mem_budget_mb: number | null;
  reference_bases: number;
  input_bytes: number;
  index_status: Record<string, boolean>;
  models: Record<string, MemoryModel>;
  /** Only present when the envelope was requested with `chunked=true`
   *  (pipeline_service.align_envelope); `total_sequences` is present only
   *  when `supported` is true, since it comes from reading the reference's
   *  .fai and there is nothing to count when chunking can't apply. */
  chunking?: { supported: boolean; total_sequences?: number };
}

/** Mirrors align_runner.ReadGroup: the @RG fields a variant caller requires. */
export interface ReadGroup {
  sample: string;
  library: string;
  platform: string;
}

export interface AlignDefaults {
  params: AlignParams;
  read_group: ReadGroup;
  aligners: { name: AlignerName; available: boolean }[];
  presets: AlignPreset[];
}

/** Which indexes a reference has. Keys are aligner names, plus "fai". */
export type IndexStatus = Record<string, boolean>;

export interface ReferenceOption {
  object_id: string;
  name: string;
  size: number;
  role: ObjectRole | null;
  indexes: IndexStatus;
  index_ids: Record<string, string>;  // aligner name → sidecar object id, for download links
}

/** One additional read set for an alignment launch: an R1 and, when paired,
 * its mate. The set's pairing follows the run's primary pair. */
export interface AdditionalReadSet {
  object_id: string;
  mate_object_id?: string | null;
}

export interface AlignRequest {
  object_id: string;
  reference_id: string;
  mate_object_id?: string | null;
  /** Ordered additional read sets, aligned alongside the primary pair. */
  additional_read_sets?: AdditionalReadSet[];
  paired: boolean;
  read_group: ReadGroup;
  params: Partial<AlignParams>;
  // "Launch anyway" from the refusal card. Skips the enqueue-time BLOCK and
  // persists on the job, where claim.lua admits it only as sole occupant.
  resource_override?: boolean;
}

/** Alignment statistics read from `samtools flagstat` during index_bam. */
export interface AlignmentFacts {
  total_reads?: number;
  mapped_reads?: number;
  mapped_pct?: number;
  properly_paired_reads?: number;
  properly_paired_pct?: number;
  duplicate_reads?: number;
  duplicate_pct?: number;
  /** STAR only: the fraction its MAPQ 255 code marks. See lib/mapq. */
  uniquely_mapped_percent?: number;
  aligned_by?: string;
  aligner_version?: string;
}

export interface ContigCoverage {
  contig: string;
  length: number;
  reads: number;
  unmapped_reads: number;
  covered_bases: number;
  coverage_pct: number;
  mean_depth: number;
  mean_baseq: number;
  mean_mapq: number;
}

export interface CoverageBoundary {
  contig: string;
  bin_start: number;
}

export interface CumulativeCoveragePoint {
  depth: number;
  fraction: number;
}

export interface BamStatsSummary {
  total_contigs: number;
  total_length: number;
  mapped_reads: number;
  unmapped_reads: number;
  mean_depth: number;
  pct_covered_1x?: number;
  pct_covered_10x?: number;
  pct_covered_30x?: number;
}

export interface MapqHistogramBucket {
  mapq: number;
  count: number;
}

export interface InsertSizeHistogramBucket {
  insert_size: number;
  count: number;
}

export interface DepthHistogramBucket {
  /** The bucket's lower bound. The final bucket is the overflow bucket. */
  depth: number;
  /** Reference positions at this depth -- not reads. */
  count: number;
}

export interface GeneBodyPoint {
  /** 0 = 5' end, 99 = 3' end. */
  percentile: number;
  /** Normalized to the curve's own maximum. */
  coverage: number;
}

export interface FeatureDistribution {
  exonic: number;
  intronic: number;
  intergenic: number;
}

export interface ReadLengthHistogramBucket {
  length_bin: number;
  count: number;
}

/** Facts produced by the run_bam_stats job. Read from ObjectDetail.facts
 * under the bam_stats_ prefix -- see BamResults.tsx. */
export interface BamStatsFacts {
  bam_stats_status?: "ok";
  bam_stats_tool_version?: string;
  bam_stats_computed_at?: string;
  bam_stats_summary?: BamStatsSummary;
  bam_stats_coverage_bins?: number[];
  bam_stats_coverage_boundaries?: CoverageBoundary[];
  bam_stats_cumulative?: CumulativeCoveragePoint[];
  bam_stats_depth_histogram?: DepthHistogramBucket[];
  bam_stats_depth_bucket_width?: number;
  bam_stats_contigs_top?: ContigCoverage[];
  bam_stats_report?: string;
  mapq_histogram?: MapqHistogramBucket[];
  /** Present only when the values are STAR's locus codes, not phred scores. */
  mapq_scale?: "star";
  insert_size_histogram?: InsertSizeHistogramBucket[];
  transcript_qc_status?: "ok";
  transcript_qc_sampled_reads?: number;
  transcript_qc_annotation?: string;
  gene_body_coverage?: GeneBodyPoint[];
  feature_distribution?: FeatureDistribution;
}

export interface ContigsPage {
  total: number;
  rows: ContigCoverage[];
}
