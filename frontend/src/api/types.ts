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
  | "transcript"
  /** Per-gene read counts for one sample. Anonymous TSV, so only the role
   * distinguishes it. */
  | "counts"
  /** Per-gene fold changes and adjusted p-values from a DE test. Also
   * anonymous TSV, and kept separate from "counts" so a results table can
   * never be fed back into a DE run as if it were input. */
  | "de_results"
  /** The GFA graph beside a de novo assembly's contigs. A role rather than a
   * sidecar: it is a result someone opens in Bandage, not scaffolding for
   * another tool. */
  | "assembly_graph";

/**
 * What kind of scaffolding a sidecar is. Distinct from ObjectRole: a role says
 * how a file is *used*, and a sidecar is not used by a person at all.
 */
export type SidecarRole =
  | "bwa-mem2-index"
  | "minimap2-index"
  | "bowtie2-index"
  | "hisat2-index"
  /**
   * One role for all eight files of STAR's genome directory. They are stored
   * flat as `<reference>.STARindex.<member>` and only become a directory when
   * a job materializes them for STAR.
   */
  | "star-index"
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
  /** Which half of the pair: 1 or 2. Null for single-end files, and for pairs
   *  linked before this field existed. */
  read_number: number | null;
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
  /**
   * Digest of the object's current facts and metadata, for comparison against
   * `ai_summary_fingerprint`. Detail-only, and null when the server did not
   * compute one -- in which case staleness is simply not claimed.
   */
  summary_fingerprint?: string | null;
}

/** One completed run, as shown in an object's History tab. Every resource
 *  field is null for a run under the 60s sampling floor -- render that as an
 *  em-dash, never as 0, since a null is the absence of a measurement rather
 *  than a measurement of zero. */
export interface ComputationRecord {
  job_type: string;
  outcome: "succeeded" | "failed" | "dead" | "cancelled";
  finished_at: string | null;
  duration_ms: number;
  queued_ms: number | null;
  threads: number | null;
  tool: string | null;
  tool_version: string | null;
  peak_rss_bytes: number | null;
  peak_cpu_percent: number | null;
  machine_cpu_model: string | null;
  machine_logical_cores: number | null;
  machine_total_ram_bytes: number | null;
  machine_platform: string | null;
  job_id: string | null;
  input_bytes: number;
}

/**
 * `produced_by` and `records` answer different questions -- "what made this
 * file" vs. "what has run on it" -- and stay separate rather than merging
 * into one list.
 *
 * `produced_by_job` is present even when `produced_by` is null: that
 * combination means the object's `produced_by_job` names a run that predates
 * computation records (2026-08-03), which reads differently from "nothing
 * ever ran" and the UI must say so.
 */
export interface ObjectComputations {
  produced_by: ComputationRecord | null;
  produced_by_job: string | null;
  records: ComputationRecord[];
  has_more: boolean;
}

export type ProvenanceStep = {
  object_id: string;
  name: string;
  kind: "spine" | "supporting";
  verb: string | null;
  tool: string | null;
  tool_version: string | null;
  job_type: string | null;
  ran_at: string | null;
  outcome: string | null;
};

export type ProvenanceNarrative = {
  markdown: string;
  gap_count: number;
  steps: ProvenanceStep[];
  materials: ProvenanceStep[];
  has_branches: boolean;
};

export type ProvenanceProse = {
  prose: string | null;
  unavailable_reason: string | null;
};

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
    // null means indeterminate -- a tool that cannot produce an honest
    // fraction (Flye, Clair3, minimap2) reports phases only. Must render
    // differently from a determinate 0, not as a bar stuck at zero.
    pct: number | null;
    phase: string;
    bytes_done: number;
    bytes_total: number;
    message: string;
    units_done: number | null;
    units_total: number | null;
    unit_label: string;
    rss_bytes: number | null;
    cpu_percent: number | null;
    peak_rss_bytes: number | null;
    peak_cpu_percent: number | null;
    phase_index: number | null;
    phase_total: number | null;
  };
  last_attempt_progress: {
    attempt: number;
    pct: number | null;
    phase: string;
    message: string;
    peak_rss_bytes: number | null;
  } | null;
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
  /** True when `options` is a set of suggestions from a vocabulary owned
   *  elsewhere (NCBI, an instrument vendor). Renders as a free-text combo
   *  rather than a <select>; see SchemaMetadataEditor. */
  open_vocabulary: boolean;
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
  | "variant"
  | "expression"
  | "assemble"
  | "reference_assembly"
  | "assembly_qc";

export interface PipelineTool {
  name: string;
  path: string | null;
  version: string | null;
  available: boolean;
  error: string | null;
  /**
   * Only set for an "on_demand" tool (see `delivery` below); null for every
   * bundled one. Distinguishes "not installed" (an offer -- show Install)
   * from "unknown" (a fault -- no docker client, or an unreachable daemon).
   * `available` is already false in both cases, but the UI must not render
   * them the same way: one is a button, the other is an error state.
   */
  install_state: "installed" | "not_installed" | "unknown" | null;
  /** Plural: fastp is both a trimmer and a QC tool. Mirrors TOOL_META. */
  pipelines: PipelineType[];
  summary: string;
  one_liner: string;
  strengths: string[];
  /**
   * Whether a job handler actually branches on this tool, independent of
   * `available` (whether the binary works). The selector must use this, not
   * `available`, to decide whether a card is selectable: `available` alone
   * would offer a choice that silently does nothing.
   *
   * This comment used to name cutadapt and Trimmomatic as the unrunnable
   * examples. That has not been true since trim_reads grew its three-way
   * dispatch, and no TOOL_META entry sets it false today -- the case that
   * still reaches here is `tool_with_meta`'s fallback, which defaults it to
   * false for a tool the backend has no metadata entry for at all.
   */
  runnable: boolean;

  /**
   * Reference data for the Software help page, from ToolMeta. Any of these
   * may be empty: a tool with no public repository or no paper is a real
   * case, and the page renders the absence rather than a dead link.
   */
  homepage: string;
  repository: string;
  citation: string;
  citation_url: string;
  license: string;
  /** How BioFlow uses this tool -- the part no upstream page can tell you. */
  usage: string;

  /**
   * How this tool reaches the running stack. "bundled" ships in the backend
   * image; "on_demand" is a pinned OCI image pulled on first use and run as
   * a sibling container (the DeepVariant shape). See
   * docs/superpowers/specs/2026-08-05-optional-tool-delivery-design.md.
   */
  delivery: "bundled" | "on_demand";
  /** The pinned image reference, only set when delivery is "on_demand". */
  image: string | null;
  /**
   * Compressed transfer size an Install button should state, only set when
   * delivery is "on_demand". Not the on-disk size after decompression --
   * DeepVariant's 2.99 GB pull becomes 8.83 GB on disk, and the download is
   * the number a user weighing their connection actually wants.
   */
  download_bytes: number | null;
}

export interface PipelineTools {
  tools: PipelineTool[];
  all_available: boolean;
}

/** An external data source. Mirrors sources.DataSource.
 *
 *  No version field, deliberately: a source has nothing to probe, and
 *  NCBI Datasets is whatever the API returned today. */
export interface DataSource {
  name: string;
  kind: "api" | "database" | "reference";
  summary: string;
  usage: string;
  homepage: string;
  docs: string;
  citation: string;
  citation_url: string;
  terms: string;
}

export interface DataSources {
  sources: DataSource[];
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

/** What a user asked for, and the jobs that served it.
 *
 * Mirrors the backend `RunKind` enum. Nothing switches exhaustively on this
 * today, which is why `assembly_download` went missing here for a while
 * without anything failing -- a reason to keep it in step deliberately rather
 * than a reason not to bother.
 */
export type RunKind =
  | "alignment"
  | "trim"
  | "sra_download"
  | "variant_calling"
  | "assembly_download"
  | "uniprot_download"
  | "quantify"
  | "differential_expression"
  | "assembly"
  | "reference_assembly";

/** Derived from member job states on the server, never stored. */
export type RunStatus =
  | "waiting"
  | "running"
  | "succeeded"
  | "failed"
  /** Finished, but an optional step (a header parse) did not succeed. */
  | "partial";

export type RunInputRole =
  | "reads"
  | "mate"
  | "reference"
  | "draft_assembly"
  | "alignment"
  | "primers"
  | "annotation"
  /** Appears many times in one run's inputs -- a DE run has one per sample. */
  | "counts";

export type RunJobRole =
  | "index"
  | "align"
  | "trim"
  | "index_bam"
  | "ingest"
  | "download"
  | "qc"
  | "call_variants"
  | "quantify"
  | "test"
  | "consensus"
  | "polish"
  | "scaffold";

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
  preset?: AlignPreset | "";
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
}

/** One input in the generated parameter form. Mirrors registry ParamField. */
export interface ParamFieldMeta {
  key: string;
  label: string;
  kind: "int" | "bool" | "select" | "text";
  default: unknown;
  help: string;
  group: "biology" | "performance";
  min: number | null;
  max: number | null;
  choices: { value: string; label: string }[];
}

export type AssemblerName = "flye" | "hifiasm" | "spades";

export interface AssemblerSchema {
  assembler: AssemblerName;
  available: boolean;
  unavailable_reason: string;
  layout: "single" | "paired";
  fields: ParamFieldMeta[];
}

export interface AssemblyParams {
  assembler: AssemblerName;
  mode: string;
  threads: number;
  iterations: number;
  /** Bases. Null when nothing in the project could say, which is the normal
   *  case for de novo work rather than a misconfiguration. */
  genome_size?: number | null;
  /** Where the number came from. "inferred" is what the dialog labels, so a
   *  guess is never shown as though it were measured. */
  genome_size_source?: "unset" | "user" | "inferred";
  /** The assembly the inferred size was read off, e.g. "R64 (GCF_000146045.2)".
   *  Names the assembly rather than the file, since every component of one
   *  download carries the same figure. */
  genome_size_from?: string;
}

export interface AssembleRequest {
  object_id: string;
  params?: Partial<AssemblyParams>;
  resource_override?: boolean;
}

export interface CompletenessDefaults {
  organism: string | null;
  /** The inferred lineage name, or null when there is nothing to infer from --
   *  the dialog then requires the user to pick one rather than guessing. */
  lineage: string | null;
  odb: string;
  /** Whether `lineage` is a genus/family-level match rather than the broad
   *  domain fallback (bacteria/eukaryota) -- the dialog says so, the same
   *  "inferred, labelled as inferred" honesty the assemble dialog's genome
   *  size uses. */
  specific: boolean;
}

export interface CompletenessRequest {
  object_id: string;
  lineage?: string | null;
  odb?: string | null;
}

export interface LineageDownloadRequest {
  lineage: string;
  odb?: string | null;
}

export interface ScaffoldRequest {
  draft_object_id: string;
  reference_object_id?: string | null;
  divergence?: string | null;
}

export interface LineageStatus {
  lineage: string;
  odb: string;
  present: boolean;
}

export interface AlignerSchema {
  aligner: AlignerName;
  fields: ParamFieldMeta[];
}

/** Mirrors resource_estimator.MemoryModel. */
export interface MemoryModel {
  index_bytes_per_ref_base: number;
  fixed_overhead_mb: number;
  bytes_per_thread_mb: number;
  index_build_multiplier: number;
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
}

/** One knob the re-planner moved. Mirrors replan_service.Change. */
export interface ReplanChange {
  name: string;
  before: number;
  after: number;
}

/**
 * Mirrors replan_service.ReplanResult.
 *
 * A tagged union rather than a nullable proposal: "nothing fits" and "there is
 * nothing here to tune" call for different prose and different next steps, and
 * collapsing both into null loses exactly the distinction the user needs.
 */
export type ReplanResult =
  | {
      kind: "proposal";
      params: Record<string, unknown>;
      estimate_mb: number;
      changes: ReplanChange[];
      note: string;
    }
  | { kind: "infeasible"; reason: string }
  | { kind: "no_knobs" };

/**
 * The `details` payload of a 422 resource refusal.
 *
 * Assembly renders the card straight from this; alignment builds the same
 * shape client-side from its envelope, so both dialogs feed one component.
 */
export interface ResourceRefusalDetails {
  estimate_mb: number;
  budget_mb: number;
  estimate_source: "measured" | "heuristic" | "declared" | "unknown";
  detail: string;
  replan: ReplanResult;
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
  // "Launch anyway" from the refusal card. Skips the enqueue-time BLOCK and
  // persists on the job, where claim.lua admits it only as sole occupant.
  resource_override?: boolean;
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
  /** Consent to a multi-gigabyte on-demand-tool download. Without it, a
   *  request against a not-yet-installed optional caller (DeepVariant) is
   *  refused with a 422 naming the download size, in `details.download_bytes`
   *  -- re-post with this set once the user has actually agreed to it. */
  install_optional?: boolean;
}

/* --- Expression: counting and differential testing ----------------------- */

/** featureCounts' -s. 0 unstranded, 1 forward, 2 reverse. */
export type Strandedness = 0 | 1 | 2;

/** Mirrors counts_runner.CountsParams. */
export interface CountsParams {
  threads: number;
  strandedness: Strandedness;
  strandedness_label: string;
  /** Counts fragments rather than reads. Wrong on paired data doubles
   * every count, which is why the dialog shows where the value came from. */
  paired: boolean;
  feature_type: string;
  attribute: string;
  count_multi_mapping: boolean;
}

export interface QuantifyDefaults {
  params: CountsParams | Record<string, never>;
  annotation_id: string | null;
  annotation_name: string | null;
  /** True when the project has several annotations and none could be picked. */
  needs_annotation: boolean;
  annotations: { id: string; name: string; kind: string }[];
  /** "alignment" when read off the aligner's --rna-strandness, else
   * "default" -- the dialog says which, because a guess and a derived value
   * deserve different confidence. */
  strandedness_source: "alignment" | "default";
  paired_source: "flagstat" | "alignment";
  available: boolean;
  max_threads: number;
}

export interface QuantifyRequest {
  bam_id: string;
  annotation_id?: string | null;
  params?: Partial<CountsParams>;
}

/** One counts file, as the DE dialog sees it before a design is chosen. */
export interface DeSample {
  object_id: string;
  name: string;
  sample: string;
  /** From the `condition` metadata field. Empty when never tagged. */
  condition: string;
  assigned_pct: number | null;
  genes_detected: number | null;
  /** Samples counted against different annotations cannot be merged. */
  annotation_sha256: string | null;
  annotation_name: string | null;
}

export interface DeDefaults {
  samples: DeSample[];
  conditions: string[];
  /** Pre-filled only when exactly two conditions are present. */
  contrast: { test: string; reference: string } | null;
  min_replicates: number;
  available: boolean;
}

export interface DeRequest {
  project_id: string;
  /** counts object id -> condition name. */
  design: Record<string, string>;
  contrast: { test: string; reference: string };
  threads?: number | null;
}

/** One gene's result. Nulls are real: DESeq2 leaves padj unset for genes it
 * filtered out of multiple-testing correction. */
export interface DeRow {
  gene: string;
  base_mean: number | null;
  log2_fold_change: number | null;
  lfc_std_error: number | null;
  stat: number | null;
  p_value: number | null;
  padj: number | null;
}

export interface DeResultsPage {
  rows: DeRow[];
  total: number;
  offset: number;
  limit: number;
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
  /** Present only when the values are STAR's locus codes, not phred scores. */
  mapq_scale?: "star";
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
  /** NCBI's platform spelling, e.g. OXFORD_NANOPORE. Written by the long-read
   *  QC path alongside the chemistry it inferred. */
  qc_platform?: string | null;
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

/**
 * A narrative summary written by the local model, if one is running.
 *
 * Entirely optional and entirely derived: every one of these keys is absent
 * unless a `summarize_object` job has succeeded, and nothing in the app
 * depends on their presence. The model and timestamp ride along because a
 * summary is only as trustworthy as the thing that wrote it and the numbers it
 * saw -- both of which can have moved on since.
 */
export interface AiSummaryFacts {
  ai_summary?: string;
  ai_summary_model?: string | null;
  /** ISO 8601, UTC. */
  ai_summary_at?: string;
  /**
   * Digest of the facts and metadata this summary was written from. Compared
   * against the object's current inputs to tell a summary that still describes
   * the file from one written before the last QC or trim run.
   */
  ai_summary_fingerprint?: string;
}

/** Same shape as AiSummaryFacts, for a differential-expression result. */
export interface DeSummaryFacts {
  ai_de_summary?: string;
  ai_de_summary_model?: string | null;
  ai_de_summary_at?: string;
  ai_de_summary_fingerprint?: string;
}

/** Same shape as AiSummaryFacts, for a VCF's call-set statistics. */
export interface VariantSummaryFacts {
  ai_variant_summary?: string;
  ai_variant_summary_model?: string | null;
  ai_variant_summary_at?: string;
  ai_variant_summary_fingerprint?: string;
}

// --- NCBI SRA ---

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

// --- NCBI unified resolve (assembly branch) ---

/** One downloadable part of an assembly. */
export interface AssemblyComponent {
  key: "genome" | "gff3" | "protein" | "cds";
  label: string;
  role: ObjectRole;
  available: boolean;
  size_bytes: number | null;
  /** Why it is unavailable. Present only when `available` is false. */
  reason: string | null;
}

export interface AssemblyResolveResponse {
  accession: string;
  organism: string | null;
  tax_id: number | null;
  strain: string | null;
  assembly_name: string | null;
  assembly_level: string | null;
  submitter: string | null;
  release_date: string | null;
  bioproject: string | null;
  paired_accession: string | null;
  total_length: number | null;
  scaffold_count: number | null;
  contig_count: number | null;
  gc_percent: number | null;
  scaffold_n50: number | null;
  components: AssemblyComponent[];
  already_downloaded: boolean;
  error: string | null;
}

/**
 * One accession, two possible answers. `kind` says which branch is populated
 * so the dialog never has to infer it from the shape.
 */
export interface NcbiResolveResponse {
  kind: string;
  sra: SraResolveResponse | null;
  assembly: AssemblyResolveResponse | null;
}

export interface AssemblyAccepted {
  run_id: string;
  download_job_ids: string[];
}

/** One candidate organism from NCBI's taxon_suggest, for autocomplete. */
export interface OrganismSuggestion {
  sci_name: string;
  tax_id: number;
  common_name: string | null;
  rank: string | null;
  group_name: string | null;
}

export interface OrganismSuggestResponse {
  suggestions: OrganismSuggestion[];
}

/**
 * A row in an organism's assembly list. Lighter than `AssemblyResolveResponse`:
 * no `components`, since that needs a CLI shellout per accession and this can
 * be a page of up to 20. Picking one assembly for its component picker goes
 * back through the existing single-accession `/ncbi/resolve` path.
 */
export interface OrganismAssemblySummary {
  accession: string | null;
  organism: string | null;
  tax_id: number | null;
  strain: string | null;
  assembly_name: string | null;
  assembly_level: string | null;
  submitter: string | null;
  release_date: string | null;
  /** NCBI's own pick for this organism: "reference genome" or
   *  "representative genome". Null for every other assembly. */
  refseq_category: string | null;
  total_length: number | null;
  scaffold_count: number | null;
  gc_percent: number | null;
  already_downloaded: boolean;
}

export interface OrganismSearchRequest {
  tax_id: number;
  sci_name: string;
  project_id?: string | null;
  assembly_page_token?: string | null;
  sra_offset?: number;
  page_size?: number;
  /** ILLUMINA | PACBIO_SMRT | OXFORD_NANOPORE, or null for everything. Only
   *  applies to sequencing runs -- an assembly has no platform of its own. */
  platform_filter?: string | null;
  /** NCBI's own assembly_level vocabulary, e.g. "Complete Genome". Only
   *  applies to the assembly list. */
  assembly_level?: string | null;
  /** Which table this request wants back. "both" is the initial search;
   *  paging either table's own pager narrows to that table alone. */
  section?: "both" | "assemblies" | "sra";
}

export interface OrganismSearchResponse {
  tax_id: number;
  sci_name: string | null;
  assemblies: OrganismAssemblySummary[];
  assemblies_next_page_token: string | null;
  sra_runs: SraRunInfo[];
  sra_total_count: number;
  sra_next_offset: number | null;
  error: string | null;
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

export interface ActiveJob {
  id: string;
  job_type: string;
  state: string;
}

export interface DeletionPreview {
  project_ids: string[];
  child_project_count: number;
  object_count: number;
  total_bytes: number;
  run_count: number;
  job_count: number;
  upload_session_count: number;
  active_jobs: ActiveJob[];
  blocked: boolean;
}

/** One output file of a prior run. `exists` is false once the file has been
 *  deleted -- the run still happened, so the row keeps its recorded name and
 *  renders as plain text rather than a dead link. */
export interface PriorRunOutput {
  object_id: string;
  name: string;
  exists: boolean;
}

/** A run that already did what a card offers. Failed runs are included and
 *  carry no outputs: a card that hid its failures would invite the same
 *  failed launch again. */
export interface PriorRun {
  run_id: string;
  finished_at: string;
  status: "succeeded" | "partial" | "failed";
  outputs: PriorRunOutput[];
}

/**
 * One pipeline offer for a data file, as rendered in the Actions tab.
 *
 * Every card is either `available` with a `launch` payload or `unavailable`
 * with a `reason` -- the two always agree, since an available card without a
 * payload would render as a button that does nothing. `why` is populated only
 * on available cards, `reason` only on unavailable ones.
 *
 * `body` is deliberately opaque. It is the *complete* JSON body for
 * `endpoint`, assembled server-side where the object id and its defaults are
 * known, and the client posts it verbatim rather than merging anything in:
 * the three launch endpoints do not share a request shape (`/variants` keys
 * on `bam_id`, the others on `object_id`), so anything the client had to add
 * would be a shape it had to know about.
 */
export interface PipelineSuggestion {
  kind: string;
  category: string;
  title: string;
  description: string;
  why: string | null;
  /**
   * "needs_install" is not blocked -- it is one click from working, and the
   * card keeps a real `launch` payload just like "available" does. It exists
   * so the UI can tell "one click from working" apart from "unavailable"
   * (a dead end with a reason): rendering a not-yet-installed optional tool
   * as unavailable would read as permanently broken and the user would never
   * learn the tool exists at all.
   */
  status: "available" | "unavailable" | "needs_install";
  reason: string | null;
  launch: { endpoint: string; body: Record<string, unknown> } | null;
  /** Set only when status is "needs_install": what pressing Launch costs. */
  requires_install: { tool: string; download_bytes: number | null } | null;
  prior_runs: PriorRun[];
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

/**
 * The profile shape is declared in `stores/profileStore.ts` and re-exported
 * here so `api/client.ts` can name a response type without a second copy
 * drifting from the first. The store is the home rather than this file
 * because the store is what has to keep the value alive across a reload --
 * everything else, including the API layer, only ever passes it through.
 */
export type { Profile } from "../stores/profileStore";

// --- Shares ---

/** Just enough to name one side of a share in a list row -- never the
 *  profile's email, details, or password status. */
export interface ShareParty {
  owner: string;
  username: string;
  emoji: string;
  colour: string;
}

export type ShareState = "offered" | "accepted" | "declined" | "withdrawn";

export interface Share {
  id: string;
  from_owner: string;
  to_owner: string;
  /** Resolved server-side. Never join `from_owner`/`to_owner` against
   *  `/profiles` on the client -- the adopted profile's owner string is the
   *  literal "local", which matches no profile id, so that join silently
   *  renders a blank sender for exactly the profile most likely to be
   *  sharing. */
  from_profile: ShareParty;
  to_profile: ShareParty;
  source_object_id: string;
  name: string;
  size: number;
  state: ShareState;
  accepted_object_id: string | null;
  message: string | null;
  created_at: string;
  updated_at: string;
}

// --- Project Q&A chat ---

export interface ConversationTurn {
  role: "user" | "assistant";
  content: string;
}

export interface ProjectConversation {
  turns: ConversationTurn[];
  compacted_summary: string | null;
}

export interface AskQuestionResponse {
  job_id: string;
}

// --- UniProt ---

/** One proteome, as the download dialog's card and picker render it. */
export type UniProtProteome = {
  id: string;
  name: string;
  taxon_id: number | null;
  strain: string | null;
  protein_count: number | null;
  is_reference: boolean;
  /** Completeness, which is what makes choosing between strains possible. */
  busco_score: number | null;
  /** The NCBI assembly this proteome's genome came from, when UniProt names
   *  one. Offered as a link to the other dialog rather than a joint download. */
  genome_assembly: string | null;
  /** Both counts, so the reviewed/unreviewed difference is visible before the
   *  download rather than discovered after it -- roughly sevenfold for human,
   *  and identical for a fully curated organism like yeast. Null when the
   *  count request failed; the card then omits the choice rather than
   *  guessing. */
  reviewed_count: number | null;
  total_count: number | null;
};

export type UniProtProtein = {
  accession: string;
  entry_id: string | null;
  name: string | null;
  organism: string | null;
  length: number | null;
  reviewed: boolean;
};

export type UniProtResolveResponse = {
  kind: "proteome" | "proteins" | "empty";
  proteome: UniProtProteome | null;
  /** Other proteomes for the same organism. Populated on both branches: behind
   *  a disclosure when a reference proteome was found, and as the whole answer
   *  when it was not (taxon 4932 has none, but 360 sit behind it). */
  candidates: UniProtProteome[];
  needs_picker: boolean;
  proteins: UniProtProtein[];
  message: string | null;
};

export type UniProtAccepted = {
  run_id: string;
  job_ids: string[];
};

export interface FeedbackSubmission {
  contact: string;
  subject: string;
  comment: string;
}

export interface Feedback extends FeedbackSubmission {
  id: string;
  created_at: string;
}

export type LocalDatabaseCategory =
  | "reference_assembly"
  | "annotation"
  | "variant_clinical"
  | "taxonomy_metadata"
  | "pipeline_tool_data"
  | "other";

export const LOCAL_DATABASE_CATEGORY_LABELS: Record<LocalDatabaseCategory, string> = {
  reference_assembly: "Reference / Assembly",
  annotation: "Annotation",
  variant_clinical: "Variant / Clinical",
  taxonomy_metadata: "Taxonomy / Metadata",
  pipeline_tool_data: "Pipeline / Tool Data",
  other: "Other",
};

export interface LocalDatabaseSubmission {
  name: string;
  url: string;
  category: LocalDatabaseCategory;
}

export interface LocalDatabaseEntry extends LocalDatabaseSubmission {
  id: string;
  created_at: string;
}

/** A known provider, offered in the add-provider form. Picking one pre-fills
 *  the base URL; it stays editable afterwards, which is how a mainland
 *  DashScope account or a non-default local port gets configured. */
export interface AiPreset {
  id: string;
  label: string;
  kind: "openai_compat" | "anthropic";
  base_url: string;
  needs_key: boolean;
}

/** A configured provider. Note what is absent: there is no field carrying the
 *  API key. `key_hint` is the masked form and `has_key` is the boolean the
 *  form needs -- the real value never leaves the backend. */
export interface AiProvider {
  id: string;
  name: string;
  kind: "openai_compat" | "anthropic";
  base_url: string;
  model: string;
  key_hint: string | null;
  has_key: boolean;
  models_cache: string[];
  status: "ok" | "failed" | "untested";
  status_reason: string | null;
  checked_at: string | null;
  /** Human labels of the task slots routed here, including "Default". */
  used_by: string[];
}

export interface AiSlot {
  name: string;
  label: string;
}

export interface AiRouting {
  default: string | null;
  /** Only explicitly-overridden slots. An absent slot means "use default". */
  slots: Record<string, string>;
  catalog: AiSlot[];
}

export interface ResourceLimits {
  max_mem_mb: number | null;
  max_cpu: number | null;
  max_threads: number | null;
  machine_mem_mb: number;
  machine_cpu: number;
  /** Kernel-enforced ceiling, or null when hard limits are off. */
  hard_mem_mb: number | null;
}

export interface ResourceLimitsIn {
  max_mem_mb: number | null;
  max_cpu: number | null;
  max_threads: number | null;
}

export interface AiFetchModelsResult {
  status: "ok" | "failed";
  models: string[];
  reason: string | null;
  detail: string | null;
}

/** Create and update share a shape, but update omits `api_key` unless the user
 *  typed a new one -- that omission is what preserves the stored key. */
export interface AiProviderInput {
  name?: string;
  kind?: "openai_compat" | "anthropic";
  base_url?: string;
  model?: string;
  api_key?: string | null;
}

export interface VersionInfo {
  version: string;
}

/* ---------------------------------------------------------------- workflows */

/** What may flow down a wire. Mirrors the backend `PortType`: `role: null`
 *  means "any role for this format", which is the honest type for a port that
 *  genuinely does not care -- QC's, for instance. */
export interface PortType {
  format: string;
  role: string | null;
}

export interface PortMeta {
  name: string;
  type: PortType;
  required: boolean;
}

/** One entry of the canvas palette, served by `/workflows/node-types`.
 *  Generated from the backend registry rather than hand-listed here, so a tool
 *  added there appears in the canvas without a frontend change. */
export interface NodeTypeMeta {
  node_type: string;
  label: string;
  inputs: PortMeta[];
  outputs: PortMeta[];
}

export interface NodePosition {
  x: number;
  y: number;
}

export interface WorkflowNode {
  node_id: string;
  kind: "input" | "action";
  /** ACTION only: keys into the palette. */
  node_type?: string | null;
  params: Record<string, unknown>;
  continue_on_failure: boolean;
  position?: NodePosition;
  /** INPUT only. The label is why input nodes are explicit rather than implied
   *  by an unwired port: "tumor reads" and "normal reads" are the same type and
   *  only a name tells them apart. */
  label?: string | null;
  accepts?: PortType | null;
}

export interface WorkflowEdge {
  from_node: string;
  from_port: string;
  to_node: string;
  to_port: string;
}

export interface WorkflowDefinition {
  id: string;
  name: string;
  description: string;
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  version: number;
}

export interface WorkflowDefinitionInput {
  name: string;
  description: string;
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
}

export interface WorkflowRunSummary {
  id: string;
  definition_id: string;
  definition_version: number;
  label: string;
  status: string;
}

/** One problem with a saved graph. `node_id`/`port` are what let the canvas
 *  mark the offending node rather than only showing a message. */
export interface GraphValidationError {
  code: string;
  message: string;
  node_id: string | null;
  port: string | null;
}

export interface SkippedRun {
  run_id: string;
  label: string;
  reason: string;
}

/** An unsaved graph derived from previous runs. `skipped` is the part that
 *  must be shown: a run the canvas cannot represent is reported, never
 *  silently dropped. */
export interface DerivedGraph {
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  skipped: SkippedRun[];
}

/** One workflow run in the activity listing.
 *
 * `status` is derived server-side from node states, never stored -- the same
 * vocabulary as `RunStatus`, so the existing STATUS_LABELS apply. The counts
 * ride along so a collapsed row reads "1 of 3" without a second request. */
export interface WorkflowRunRow {
  id: string;
  definition_id: string;
  definition_version: number;
  project_id: string;
  label: string;
  status: RunStatus;
  node_total: number;
  node_done: number;
  node_failed: number;
  created_at: string;
  updated_at: string;
}

export interface WorkflowNodeJob {
  job_id: string;
  /** Null when the job has been pruned by the 30-day TTL. */
  type: string | null;
  state: JobState | null;
  progress: JobSummary["progress"] | null;
  error: { code: string; message: string } | null;
}

export interface WorkflowNodeRow {
  node_id: string;
  kind: "input" | "action";
  node_type: string | null;
  label: string;
  state: "pending" | "running" | "succeeded" | "failed" | "cancelled" | "skipped";
  attempt: number;
  /** Set only for the 9 node types that create a PipelineRun. */
  run_id: string | null;
  jobs: WorkflowNodeJob[];
  outputs: string[];
}

export interface WorkflowRunDetail {
  id: string;
  definition_id: string;
  label: string;
  status: RunStatus;
  nodes: WorkflowNodeRow[];
}
