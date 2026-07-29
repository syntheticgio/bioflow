export interface Project {
  id: string;
  name: string;
  slug: string;
  description: string;
  parent_id: string | null;
  metadata: Record<string, unknown>;
  tags: string[];
  object_count: number;
  total_bytes: number;
  archived: boolean;
  created_at: string;
  updated_at: string;
}

export interface Breadcrumb {
  id: string;
  name: string;
}

export interface ProjectDetail extends Project {
  breadcrumbs: Breadcrumb[];
}

export type ObjectStatus =
  | "uploading"
  | "hashing"
  | "ingesting"
  | "ready"
  | "error"
  | "missing";

export interface FormatInfo {
  kind: string;
  compression: string;
  confidence: string;
  extension_says: string | null;
  magic_says: string | null;
  detected_at: string | null;
}

/** How a file is used, when its format cannot say. Null = derive from format. */
export type ObjectRole =
  | "reference"
  | "trimmed_reads"
  | "alignment"
  | "variants"
  /** An assembly's published annotation (GFF3). */
  | "annotation"
  /** Amino acid FASTA. Distinct from "reference" so it never reaches an
   * aligner's reference picker -- both are FASTA. */
  | "protein"
  /** CDS / transcript nucleotide FASTA. Same hazard as "protein". */
  | "transcript";

/**
 * What kind of scaffolding a sidecar is. Distinct from ObjectRole: a role says
 * how a file is *used*, and a sidecar is not used by a person at all.
 */
export type SidecarRole =
  | "bwa-mem2-index"
  | "minimap2-index"
  | "fai"
  | "bai"
  /** The tabix index beside a bgzipped VCF -- to a VCF what bai is to a BAM. */
  | "tbi";

export interface DataObject {
  id: string;
  project_id: string;
  name: string;
  size: number;
  status: ObjectStatus;
  blob_sha256: string | null;
  format: FormatInfo;
  facts: Record<string, unknown>;
  metadata: Record<string, unknown>;
  tags: string[];
  role: ObjectRole | null;
  /** Objects this one was produced from. Two entries for a paired trim. */
  derived_from: string[];
  produced_by_job: string | null;
  /** The other half of a paired-end run, if known. */
  mate_object_id: string | null;
  /** The file this one accompanies. Set only on scaffolding such as indexes. */
  sidecar_of: string | null;
  sidecar_role: SidecarRole | null;
  source: Record<string, unknown>;
  error: { code: string; message: string; at: string } | null;
  created_at: string;
  updated_at: string;
}

export interface Blob {
  sha256: string;
  size: number;
  state: string;
  storage: string;
  rel_path: string | null;
  external_path: string | null;
  ref_count: number;
  last_verified_at: string | null;
}

export interface ObjectDetail extends DataObject {
  blob: Blob | null;
}

export interface SystemStats {
  storage: {
    ok: boolean;
    detail: string;
    path: string;
    /**
     * Docker Desktop's VirtioFS reports the statfs of the share root, not the
     * external drive, so `reliable` is false and these are not shown as the
     * drive's numbers. See system.py.
     */
    disk: {
      total_bytes: number;
      used_bytes: number;
      free_bytes: number;
      percent_used: number;
      reliable: boolean;
    } | null;
    /** Bytes this library occupies. Summed over blobs, so dedup is accounted for. */
    library_bytes: number;
  };
  counts: { projects: number; objects: number; blobs: number };
  queue: QueueStats | null;
}

export interface QueueStats {
  ready: number;
  delayed: number;
  running: number;
  by_class: Record<string, number>;
  workers: number;
}

export type JobState =
  | "pending"
  | "queued"
  | "delayed"
  /** Held until every job it depends on has succeeded. */
  | "blocked"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "dead";

export type JobClass =
  | "user_interactive"
  | "user_background"
  | "maintenance"
  | "compute"
  | "bulk";

export interface JobSummary {
  id: string;
  type: string;
  job_class: JobClass;
  state: JobState;
  payload: Record<string, unknown>;
  attempts: number;
  max_attempts: number;
  progress: {
    pct: number;
    phase: string;
    bytes_done: number;
    bytes_total: number;
    message: string;
  };
  result: Record<string, unknown> | null;
  error: { code: string; message: string; retryable: boolean } | null;
  timing: {
    enqueued_at: string | null;
    started_at: string | null;
    finished_at: string | null;
    duration_ms: number | null;
  };
  resources: { cpu: number; mem_mb: number; io: string };
  cancel_requested: boolean;
  project_id: string | null;
  object_id: string | null;
  parent_job_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface TimingEstimate {
  known: boolean;
  estimate_ms?: number;
  samples: number;
  needed?: number;
  r_squared?: number;
  throughput_mb_s?: number | null;
}

export interface SystemLoad {
  state: "OPEN" | "THROTTLED" | "CLOSED";
  admitted_classes: string[];
  ramping?: boolean;
  cpu: {
    percent: number;
    budget?: number | null;
    load1_normalized?: number;
    count?: number;
    load1?: number;
  };
  memory: {
    percent: number;
    available_bytes: number;
    budget_bytes?: number | null;
    total_bytes?: number;
    swap_in_mb_s?: number;
  };
  disk: { free_bytes: number; free_percent?: number; percent_used?: number } | null;
  governor_active: boolean;
}

export interface ScheduleInfo {
  name: string;
  job_type: string;
  interval_seconds: number;
  job_class: JobClass;
  payload: Record<string, unknown>;
  enabled: boolean;
  catchup: boolean;
  last_run_at: string | null;
  next_run_at: string | null;
  last_job_id: string | null;
}

export interface OverdueSchedule {
  name: string;
  interval_seconds: number;
  last_run_at: string;
  seconds_overdue: number;
}

export interface UploadSessionInfo {
  id: string;
  project_id: string;
  filename: string;
  total_size: number;
  chunk_size: number;
  total_chunks: number;
  state: "open" | "assembling" | "hashing" | "completed" | "aborted" | "expired";
  received_chunks: number;
  received_bytes: number;
  missing_chunks: number[];
  resulting_object_id: string | null;
  resulting_sha256: string | null;
  created_at: string;
  updated_at: string;
}

export interface UploadCreated {
  dedup_hit: boolean;
  session: UploadSessionInfo | null;
  object: DataObject | null;
}

export interface CompleteAccepted {
  session_id: string;
  object_id: string;
  job_id: string;
}

export interface RegisterAccepted {
  object: DataObject;
  job_id: string;
}

export interface MetadataField {
  key: string;
  label: string;
  type: "text" | "number" | "integer" | "boolean" | "enum" | "date";
  options: string[];
  unit: string | null;
  help: string | null;
  group: string;
  suggested: boolean;
}

export interface MetadataSchema {
  kind: string | null;
  role: ObjectRole | null;
  groups: { group: string; fields: MetadataField[] }[];
}

export interface SearchResults {
  objects: DataObject[];
  total: number;
  has_more: boolean;
  next_cursor: string | null;
}

export interface FacetValue {
  value: string;
  count: number;
}

export interface Facets {
  formats: FacetValue[];
  statuses: FacetValue[];
  tags: FacetValue[];
  metadata_keys: { key: string; count: number }[];
}

export interface SearchParams {
  q?: string;
  projectId?: string;
  kind?: string[];
  status?: string[];
  tag?: string[];
  meta?: string[];
  limit?: number;
  cursor?: string;
}

export interface BulkResult {
  matched: number;
  modified: number;
  warnings?: { key: string; message: string }[];
}

export interface ApiError {
  code: string;
  message: string;
  details?: Record<string, unknown>;
}

// --- Pipelines ---

export type PipelineType =
  | "trim"
  | "align"
  | "qc"
  | "utility"
  | "download"
  | "variant";

export interface PipelineTool {
  name: string;
  path: string | null;
  version: string | null;
  available: boolean;
  error: string | null;
  /** Plural: fastp is both a trimmer and a QC tool. Mirrors TOOL_META. */
  pipelines: PipelineType[];
  summary: string;
  strengths: string[];
  /**
   * Whether a job handler actually branches on this tool, independent of
   * `available` (whether the binary works). cutadapt and Trimmomatic probe
   * as available -- real, working binaries -- but trim_reads has no code
   * path for either yet. The selector must use this, not `available`, to
   * decide whether a card is selectable: `available` alone would offer a
   * choice that silently does nothing.
   */
  runnable: boolean;
}

export interface PipelineTools {
  tools: PipelineTool[];
  all_available: boolean;
}

/** Mirrors fastp_runner.TrimParams. Nulls mean "let fastp decide". */
export interface TrimParams {
  quality_threshold: number;
  unqualified_percent_limit: number;
  min_length: number;
  trim_poly_g: boolean | null;
  trim_poly_x: boolean;
  dedup: boolean;
  detect_adapter_for_pe: boolean;
  adapter_r1: string | null;
  adapter_r2: string | null;
  threads: number;
  compression: number;
}

/** Mirrors cutadapt_runner.CutadaptParams. */
export interface CutadaptParams {
  quality_cutoff: number;
  min_length: number;
  adapter_r1: string | null;
  adapter_r2: string | null;
  threads: number;
}

/** Mirrors trimmomatic_runner.TrimmomaticParams. */
export interface TrimmomaticParams {
  quality_leading: number;
  quality_trailing: number;
  sliding_window_size: number;
  sliding_window_quality: number;
  min_length: number;
  adapter_file: string | null;
  threads: number;
}

export type TrimToolParams = TrimParams | CutadaptParams | TrimmomaticParams;

export interface TrimDefaults {
  params: TrimToolParams;
  max_threads: number;
}

export interface MateSuggestion {
  object_id: string;
  name: string;
  mate: "R1" | "R2" | null;
}

/** What a user asked for, and the jobs that served it. */
export type RunKind = "alignment" | "trim" | "sra_download" | "variant_calling";

/** Derived from member job states on the server, never stored. */
export type RunStatus =
  | "waiting"
  | "running"
  | "succeeded"
  | "failed"
  /** Finished, but an optional step (a header parse) did not succeed. */
  | "partial";

export type RunInputRole = "reads" | "mate" | "reference";

export type RunJobRole =
  | "index"
  | "align"
  | "trim"
  | "index_bam"
  | "ingest"
  | "download"
  | "qc"
  | "call_variants";

export interface RunInput {
  object_id: string;
  name: string;
  role: RunInputRole;
}

export interface RunSummary {
  id: string;
  kind: RunKind;
  project_id: string;
  label: string;
  status: RunStatus;
  inputs: RunInput[];
  params: Record<string, unknown>;
  /** Which tool ran a trim run. Null for non-trim runs. */
  tool: string | null;
  outputs: string[];
  created_at: string;
  updated_at: string;
}

export interface RunMemberJob {
  job_id: string;
  role: RunJobRole;
  /** True when this run reused a job another run created. */
  shared: boolean;
  /** Null once the job has been pruned by the 30-day TTL. */
  type: string | null;
  state: JobState | null;
  progress: JobSummary["progress"] | null;
  error: { code: string; message: string; retryable: boolean } | null;
  created_at: string | null;
}

export interface RunDetail extends RunSummary {
  jobs: RunMemberJob[];
}

export type AlignerName = "bwa-mem2" | "minimap2";

/** minimap2 presets. The wrong one for long reads aligns poorly rather than failing. */
export type AlignPreset = "map-ont" | "map-pb" | "map-hifi" | "lr:hq" | "sr";

/** Mirrors align_runner.AlignParams. */
export interface AlignParams {
  aligner: AlignerName;
  preset: AlignPreset | "";
  threads: number;
  sort_memory_mb: number;
  mark_duplicates: boolean;
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
}

export interface AlignRequest {
  object_id: string;
  reference_id: string;
  mate_object_id?: string | null;
  paired: boolean;
  read_group: ReadGroup;
  params: Partial<AlignParams>;
}

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
  bam_stats_contigs_top?: ContigCoverage[];
  bam_stats_report?: string;
  mapq_histogram?: MapqHistogramBucket[];
  insert_size_histogram?: InsertSizeHistogramBucket[];
}

export interface ContigsPage {
  total: number;
  rows: ContigCoverage[];
}

/** One side of the before/after comparison in a fastp report. */
export interface TrimSide {
  total_reads: number | null;
  total_bases: number | null;
  q20_rate: number | null;
  q30_rate: number | null;
  gc_content: number | null;
  read1_mean_length: number | null;
  read2_mean_length: number | null;
}

export interface TrimReport {
  tool: string;
  tool_version: string | null;
  sequencing: string | null;
  before: TrimSide;
  after: TrimSide;
  filtering: {
    passed_reads: number | null;
    low_quality_reads: number | null;
    too_many_n_reads: number | null;
    too_short_reads: number | null;
  };
  duplication_rate: number | null;
  insert_size_peak: number | null;
  adapters?: {
    trimmed_reads: number | null;
    trimmed_bases: number | null;
    read1_sequence: string | null;
    read2_sequence: string | null;
  };
}

/**
 * QC facts written onto an object by a `run_qc` job.
 *
 * Flat and `qc_`-prefixed rather than nested under one key, because they are
 * merged into the same `facts` dict as everything else the ingest and the
 * pipelines record. `TrimSide` is reused for the measurements: with filtering
 * disabled there is only the one state, but it is the same set of numbers.
 */
export interface QcFacts {
  qc_tool?: string;
  qc_tool_version?: string | null;
  qc_sequencing?: string | null;
  qc_before_filtering?: TrimSide;
  qc_duplication_rate?: number | null;
  qc_insert_size_peak?: number | null;
  qc_adapters?: {
    read1_sequence: string | null;
    read2_sequence: string | null;
  };
  /** Paths relative to the report route, absent when the tool did not run. */
  qc_fastp_report?: string;
  qc_fastqc_report?: string;
  qc_status?: string;

  // NanoPlot facts, written by the long-read QC path (Nanopore/PacBio)
  // instead of the fastp/FastQC pair above. N50 is this run's headline
  // number the way Q30 is for a short-read one.
  qc_total_reads?: number | null;
  qc_total_bases?: number | null;
  qc_mean_read_length?: number | null;
  qc_median_read_length?: number | null;
  qc_read_length_n50?: number | null;
  qc_read_length_stdev?: number | null;
  qc_mean_quality?: number | null;
  qc_median_quality?: number | null;
  qc_nanoplot_report?: string;
  /** Inferred by qc_stats.infer_chemistry; see ReadChemistry on the backend. */
  qc_read_chemistry?: string;
  qc_read_chemistry_reason?: string;
}

// --- NCBI SRA ---

/** NCBI's own platform spellings, as they appear on the SRA record. */
export type SraPlatform = "ILLUMINA" | "PACBIO_SMRT" | "OXFORD_NANOPORE";

/** One sequencing run: the unit that can actually be downloaded. */
export interface SraRunInfo {
  accession: string;
  experiment: string | null;
  sample: string | null;
  study: string | null;
  bioproject: string | null;
  biosample: string | null;
  platform: string | null;
  instrument: string | null;
  library_strategy: string | null;
  library_layout: string | null;
  library_source: string | null;
  spots: number | null;
  bases: number | null;
  /** Archive size from NCBI, not an estimate. Drives the size column. */
  bytes: number | null;
  organism: string | null;
  title: string | null;
  sample_attributes: Record<string, string>;
  /** Already in this project. Shown greyed out rather than hidden. */
  already_downloaded: boolean;
}

export interface SraHierarchyNode {
  accession: string;
  kind: string;
  title: string | null;
  platform: string | null;
  organism: string | null;
  child_count: number;
  total_bases: number | null;
}

export interface SraResolveResponse {
  accession: string;
  kind: string;
  title: string | null;
  organism: string | null;
  hierarchy: SraHierarchyNode[];
  runs: SraRunInfo[];
  total_run_count: number;
  total_bytes_estimate: number | null;
  /** The study holds more runs than the server will resolve in one go. */
  truncated: boolean;
  /** Set on "nothing found" and on a filter that excluded everything. */
  error: string | null;
}

export interface SraDownloadRequest {
  project_id: string;
  run_accessions: string[];
  run_qc?: boolean;
}

export interface SraAccepted {
  run_id: string;
  download_job_ids: string[];
  /** Runs already in flight, so no new job was created for them. */
  skipped: string[];
}

export interface TrimRequest {
  object_id: string;
  mate_object_id?: string | null;
  paired?: boolean;
  params?: Partial<TrimToolParams>;
  tool?: string;
}

export interface JobLog {
  job_id: string;
  exists: boolean;
  lines: string[];
  truncated: boolean;
  size?: number;
}
