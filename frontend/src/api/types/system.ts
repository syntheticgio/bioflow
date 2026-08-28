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

/** Which gate stopped the head-of-queue job, and the two numbers it
 *  compared. `need`/`free` are cores for cpu and MB for mem; the class gate
 *  carries `class`/`admitted` instead (#457). */
export interface BlockedReason {
  gate: "class" | "cpu" | "mem" | "io";
  need: number | null;
  free: number | null;
  class: string | null;
  admitted: string[] | null;
}

export interface SystemLoadNode {
  node_id: string;
  running: number;
  queued: number;
  cpu: number;
  mem_mb: number;
  workers: number;
  /** False when a ready queue exists for a node id that never enrolled. */
  known: boolean;
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
  nodes: SystemLoadNode[];
  nodes_error?: string;
  blocked_reason?: BlockedReason | null;
}

export interface NodeInfo {
  node_id: string;
  workers: number;
  online_workers: number;
  running_jobs: number;
  queued_jobs: number;
  slots: number;
  online: boolean;
  reserved: {
    cpu: number;
    mem_mb: number;
    io_heavy: number;
  };
  hostname?: string;
  registered_at?: string | null;
  enrollment?: string;
  image_digest: string | null;
  version: string | null;
  updatable: boolean;
  /** Whether this node reads the primary's storage, proven by round-trip probe.
   *  Tri-state: `null` means never probed, which is not the same as `false`,
   *  probed and not shared. A node that has never been asked can be probed;
   *  one that answered no needs its storage fixed. */
  storage_shared: boolean | null;
  storage_location: string | null;
  storage_checked_at: string | null;
}

/** One node's result within a fleet-wide storage sweep.
 *
 *  Five outcomes, and only `shared` and `not_shared` mean the probe actually
 *  ran. The other three are reasons it could not, so they leave the node's
 *  recorded status untouched -- a node that was merely powered off is not a
 *  verified negative. */
export interface NodeStorageOutcome {
  node_id: string;
  outcome:
    | "shared"
    | "not_shared"
    | "unreachable"
    | "not_probeable"
    | "no_recorded_path";
  storage_shared: boolean | null;
  storage_location: string | null;
  detail: string;
}

export interface NodeStorageSweep {
  nodes: NodeStorageOutcome[];
  /** How many nodes the probe actually ran against. */
  checked: number;
  total: number;
}

export interface NodeUpdateStatus {
  task_id: string;
  status: "updating" | "success" | "failed";
  phase: string;
  message: string;
  pct: number | null;
  node_id: string;
  host: string;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
}

export interface CurrentVersion {
  image_digest: string | null;
  version: string;
}

export interface NodeProvisionRequest {
  host: string;
  port: number;
  username: string;
  password?: string | null;
  private_key?: string | null;
  node_name: string;
  storage_location: string;
  worker_replicas: number;
}

export interface NodeProvisionStatus {
  task_id: string;
  status: "provisioning" | "success" | "failed";
  phase: string;
  message: string;
  pct: number | null;
  node_name: string;
  host: string;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
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

export interface GeneralSettings {
  feedback_enabled: boolean;
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

export interface VersionInfo {
  version: string;
}

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
