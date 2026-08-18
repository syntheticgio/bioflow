export type ObjectStatus =
  | "uploading"
  | "hashing"
  | "ingesting"
  | "ready"
  | "error"
  | "missing";

/** Where a file's bytes are right now. */
export type Locality = "local" | "remote";

/** The address an offloaded file is fetched back from. */
export interface RemoteSource {
  /** An SRA run accession (ERR/SRR/DRR). */
  accession: string;
  /** Set only for sources addressed by more than an accession. */
  component: string | null;
  /** What the source reported when the bytes were last held, so the UI can
   *  warn about the size of a re-download before starting one. */
  size: number;
}

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
  /** Whether the bytes are on this machine. Deliberately separate from
   *  `status`: an offloaded file stays "ready", so it keeps appearing in the
   *  reference picker and in Actions suggestions. Only its content is gone. */
  locality: Locality;
  /** Where an offloaded file can be fetched back from. Null while local. */
  remote_source: RemoteSource | null;
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

/** What GC to expect for a file's reads, and what said so. Null when nothing
 *  in the project or the genome table can say. */
export interface ExpectedGc {
  percent: number;
  /** "reference" (measured from a project file) or "table" (published). */
  source: string;
  /** Shown beside the curve; always names its source. */
  attribution: string;
}

export interface ObjectDetail extends DataObject {
  blob: Blob | null;
  /**
   * Digest of the object's current facts and metadata, for comparison against
   * `ai_summary_fingerprint`. Detail-only, and null when the server did not
   * compute one -- in which case staleness is simply not claimed.
   */
  summary_fingerprint?: string | null;
  /** What GC to expect, when anything can say. Null (not undefined) when the
   *  backend resolver found nothing -- matches the Python API's optional
   *  Pydantic field, which serializes an unset value as JSON null. */
  expected_gc: ExpectedGc | null;
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

/**
 * One numbered row of "How this file was made".
 *
 * `name` is already formatted for display and covers every object the row is
 * about -- "mate_1.fastq and mate_2.fastq" when one job produced both -- with
 * `names` holding them separately for anything that needs the parts. Rows are
 * merged server-side on the producing job, so two files on one row is a claim
 * the record supports, not a guess.
 */
export type ProvenanceStep = {
  object_id: string;
  name: string;
  names: string[];
  kind: "spine" | "supporting";
  verb: string | null;
  tool: string | null;
  tool_version: string | null;
  job_type: string | null;
  ran_at: string | null;
  outcome: string | null;
  params: Record<string, unknown>;
  gaps: string[];
  used_by: string | null;
};

export type ProvenanceGap = {
  label: string;
  object_id: string | null;
};

/**
 * `markdown` backs the Copy report button only -- the tab renders from
 * `lineage`, which is the same facts as structured rows. `steps` and
 * `materials` are `lineage` partitioned by kind, kept for callers that want
 * one or the other.
 */
export type ProvenanceNarrative = {
  markdown: string;
  gap_count: number;
  lineage: ProvenanceStep[];
  steps: ProvenanceStep[];
  materials: ProvenanceStep[];
  gaps: ProvenanceGap[];
  has_branches: boolean;
  /** Set when the object opened is a sidecar (e.g. a `.bai`/`.fai`) and the
   * lineage shown is its parent's instead -- names the sidecar so the tab
   * can say so. */
  redirected_from_name: string | null;
};

export type ProvenanceProse = {
  prose: string | null;
  unavailable_reason: string | null;
};

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

export interface ExtractedSequence {
  reference_id: string | null;
  reference_name: string | null;
}
