import type { Locality, ObjectRole } from "./object";
import type { AppliedParameterSetIn } from "./parameter-set";
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
  kmer_size?: number;
  window_size?: number;
  min_chain_score?: number;
  max_gap?: number;
  secondary_ratio?: number;
  max_secondary?: number;
  secondary_mode?: "enabled" | "disabled";
  soft_clip_supplementary?: boolean;
  cs_mode?: "short" | "long";
  emit_md?: boolean;
  sensitivity?: string;
  local?: boolean;
  minins?: number;
  maxins?: number;
  orientation?: "FR" | "RF" | "FF";
  dovetail?: boolean;
  no_contain?: boolean;
  no_overlap?: boolean;
  no_mixed?: boolean;
  no_discordant?: boolean;
  report_k?: number;
  report_all?: boolean;
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
  /** "remote" means picking this reference downloads it first. */
  locality: Locality;
  indexes: IndexStatus;
  /** Aligner name (plus "fai") → sidecar object id, for download links.
   * Only built indexes appear; optional so a payload without it degrades to
   * a missing download link rather than a crash. */
  index_ids?: Record<string, string>;
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
  /** Present only when the dialog applied a saved parameter set. */
  from_parameter_set?: AppliedParameterSetIn;
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
  /** Facts produced by run_feature_coverage. See FeatureCoverage.tsx. */
  feature_coverage_status?: "ok";
  feature_coverage_tool_version?: string;
  feature_coverage_computed_at?: string;
  feature_coverage_feature_count?: number;
  feature_coverage_zero_features?: number;
  feature_coverage_median_breadth?: number;
  feature_coverage_annotation_id?: string;
  /** Filename of the report on disk, e.g. "coverage.json" -- truthy iff the
   * job has run. Not a path to fetch from directly; see
   * api.featureCoverageReport, which hits the report endpoint by object id. */
  feature_coverage_report?: string;

  /** Facts produced by the mosdepth coverage job. See CoverageDepth.tsx.
   * Distinct from the feature_coverage_* block above: that is per annotated
   * feature via bedtools, this is per window (or per target region) via
   * mosdepth. */
  coverage_status?: "ok";
  coverage_tool_version?: string;
  coverage_computed_at?: string;
  coverage_mode?: "windows" | "regions";
  coverage_mean_depth?: number;
  coverage_max_depth?: number;
  coverage_reference_length?: number;
  coverage_bases_covered?: number;
  coverage_contig_count?: number;
  /** Set only in region mode; the window count is set only in windowed mode. */
  coverage_region_count?: number;
  coverage_regions_id?: string;
  coverage_window_count?: number;
  coverage_pct_at_1x?: number;
  coverage_pct_at_10x?: number;
  coverage_pct_at_30x?: number;
  /** Filename of the report on disk -- truthy iff the job has run. Fetched
   * through api.coverageReport by object id, not by this path. */
  coverage_report?: string;

  /** Facts produced by the gc_bias job. See GcBiasChart.tsx. Needs both this
   * BAM's windowed coverage and its reference's gc_tracks, so it is a
   * separate job/fact rather than folded into coverage_* above.
   * "empty" means the job ran successfully but every window's GC was None
   * (an all-N reference) -- distinct from "ok" so BamResults.tsx's render
   * gate (`=== "ok"`) doesn't attempt to draw an empty chart. */
  gc_bias_status?: "ok" | "empty";
  gc_bias_curve?: GcBiasBin[];
  /** True when the reference's gc_tracks fact was truncated to
   * MAX_STORED_CONTIGS (gc_tracks.py), so this curve covers only that
   * subset of a fragmented assembly's contigs. */
  gc_bias_partial?: boolean;

  /** Facts produced by the same gc_bias job, for the per-contig blobplot
   * (ContigBlobChart.tsx). Always "ok" when set -- unlike gc_bias_status,
   * there is no "empty" case: cap_by_cumulative_length always returns at
   * least the contigs with usable GC, and the chart's own empty-report
   * guard (`!data.contigs.length`) handles the degenerate case instead. */
  gc_blob_status?: "ok";
  /** Filename of the per-contig report on disk -- fetched through
   * api.gcBlobReport by object id, not by this path. */
  gc_blob_report?: string;
  gc_blob_contig_count?: number;
  /** Contigs omitted by the cumulative-length cap (V4); MUST drive an
   * always-visible line in ContigBlobChart when non-zero. */
  gc_blob_dropped_count?: number;
}

export interface ContigsPage {
  total: number;
  rows: ContigCoverage[];
}

/** One row of feature_coverage_runner.parse_coverage's `features` array. */
export interface FeatureCoverageRow {
  name: string;
  type: string;
  seq_id: string;
  start: number;
  end: number;
  strand: string;
  read_count: number;
  bases_covered: number;
  length: number;
  /** Fraction of the feature's length covered by at least one read, 0.0-1.0. */
  breadth: number;
}

/**
 * `GET /pipelines/feature-coverage/{object_id}/report`'s full body --
 * feature_coverage_runner.parse_coverage's return shape verbatim. No
 * pagination: capped server-side at MAX_FEATURES_IN_REPORT (10,000) with
 * `truncated` set when the cap was hit. `feature_count` (and
 * `features_zero_coverage`/`median_breadth`) are computed over ALL features
 * before the cap is applied, so `feature_count` can exceed `features.length`
 * when `truncated` is true -- it is the true total, not the included count.
 */
export interface FeatureCoverageReport {
  feature_count: number;
  features_zero_coverage: number;
  /** 0.0-1.0 fraction, median across all included features. */
  median_breadth: number;
  truncated: boolean;
  /** Pre-sorted ascending by breadth (worst coverage first), then name. */
  features: FeatureCoverageRow[];
}

/** One window (or one target region) from a mosdepth run. */
export interface CoverageWindow {
  start: number;
  end: number;
  /** Mean read depth across the interval. */
  depth: number;
  /**
   * Carried through from a 4-column target BED, so it is set on a region-mode
   * report and null on a windowed one -- mosdepth has no name to propagate
   * for a window the app generated itself.
   */
  name: string | null;
}

/** One contig's row in a mosdepth summary. */
export interface CoverageContig {
  name: string;
  length: number;
  bases: number;
  mean: number;
  min: number;
  max: number;
}

/**
 * `GET /pipelines/coverage/{object_id}/report`'s full body --
 * mosdepth_runner.build_report's return shape verbatim.
 *
 * `mode` distinguishes the two runs that produce identical row shapes:
 * "windows" is the uniform tiling the app generates from the reference's
 * contig lengths, "regions" is a user-supplied target BED. `window_count` is
 * null in region mode, where the row count is the BED's and not a tiling
 * parameter at all.
 */
export interface CoverageReport {
  mode: "windows" | "regions";
  contigs: CoverageContig[];
  total: CoverageContig | null;
  /** Keyed by contig name, in the reference's own order. */
  regions: Record<string, CoverageWindow[]>;
  /** Cumulative fraction of bases at >= each depth, across the reference. */
  dist: Record<string, number>;
  window_count: number | null;
}

/**
 * One fixed-width GC bin of `gc_coverage.bias_curve`. Bins with no observed
 * windows are omitted by the backend, not zero-filled -- see
 * GcBiasChart.tsx.
 */
export interface GcBiasBin {
  gc_min: number;
  gc_max: number;
  /** Width-weighted mean depth of every window whose GC falls in this bin. */
  mean_depth: number;
  window_count: number;
}

/** One contig's aggregate GC and depth, for ContigBlobChart's scatter --
 * gc_coverage.per_contig's row shape verbatim. */
export interface GcBlobContig {
  contig: string;
  /** Base-weighted GC percentage across this contig's windows; null when
   * every window was unscoreable (all-N). */
  gc: number | null;
  mean_depth: number;
  length: number;
  window_count: number;
}

/**
 * `GET /pipelines/gc-bias/{object_id}/report`'s full body --
 * gc_coverage_handlers.compute_gc_bias's gc_blob.json verbatim.
 *
 * `contigs` is capped by cumulative length (the longest contigs covering
 * 99% of total bases, per V4) -- `dropped_count` is how many shorter
 * contigs were omitted, and MUST be shown whenever non-zero: a contaminant
 * is often many small contigs, so the cap can drop exactly the cluster this
 * chart exists to find, and a clean-looking plot must stay distinguishable
 * from one whose contamination was truncated away.
 */
export interface GcBlobReport {
  contigs: GcBlobContig[];
  dropped_count: number;
  kept_count: number;
}
