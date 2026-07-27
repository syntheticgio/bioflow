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
export type ObjectRole = "reference" | "trimmed_reads" | "alignment";

/**
 * What kind of scaffolding a sidecar is. Distinct from ObjectRole: a role says
 * how a file is *used*, and a sidecar is not used by a person at all.
 */
export type SidecarRole = "bwa-mem2-index" | "minimap2-index" | "fai" | "bai";

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

export interface PipelineTool {
  name: string;
  path: string | null;
  version: string | null;
  available: boolean;
  error: string | null;
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

export interface TrimDefaults {
  params: TrimParams;
  max_threads: number;
}

export interface MateSuggestion {
  object_id: string;
  name: string;
  mate: "R1" | "R2" | null;
}

export type AlignerName = "bwa-mem2" | "minimap2";

/** minimap2 presets. The wrong one for long reads aligns poorly rather than failing. */
export type AlignPreset = "map-ont" | "map-pb" | "sr";

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

export interface TrimRequest {
  object_id: string;
  mate_object_id?: string | null;
  paired?: boolean;
  params?: Partial<TrimParams>;
}

export interface JobLog {
  job_id: string;
  exists: boolean;
  lines: string[];
  truncated: boolean;
  size?: number;
}
