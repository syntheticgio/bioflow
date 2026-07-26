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
export type ObjectRole = "reference";

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
    disk: {
      total_bytes: number;
      used_bytes: number;
      free_bytes: number;
      percent_used: number;
    } | null;
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
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "dead";

export type JobClass =
  | "user_interactive"
  | "user_background"
  | "maintenance"
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
