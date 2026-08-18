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

/**
 * A summary pair (median/p90) over some measurement, for the Reference →
 * Metrics page. Both are null when there are no measurements: a run under
 * the 60s sampling floor has no peak to summarize, and a job type no one
 * has run yet has no durations at all. Render null as an em-dash, never as
 * 0.
 */
export interface MetricSummary {
  median: number | null;
  p90: number | null;
}

/** One job type's row on the Reference → Metrics page. */
export interface JobTypeMetrics {
  job_type: string;
  /** Every recorded run, grouped by outcome — failures included. */
  outcomes: {
    succeeded: number;
    failed: number;
    dead: number;
    cancelled: number;
  };
  duration_ms: MetricSummary;
  input_bytes: MetricSummary;
  peak_rss_bytes: MetricSummary;
  /** Opportunistic: null until pipelines record a read count. */
  read_count: MetricSummary;
  /** Which binaries these runs used, most-used first. */
  tools: { name: string | null; version: string | null; runs: number }[];
}

/**
 * Aggregated computation cost, from GET /metrics.
 *
 * Durations, sizes, memory and read counts summarize the most recent
 * successful runs (the same window the predictive models fit); `totals` and
 * per-type `outcomes` count every recorded run, failures included. The two
 * deliberately describe different windows — a metrics page that hid
 * failures would be a status page for a rosier app.
 */
export interface MetricsStats {
  min_samples: number;
  resource_floor_ms: number;
  totals: Record<string, number>;
  types: JobTypeMetrics[];
}

/**
 * One recorded run, for the Metrics page's per-job-type tables.
 *
 * Every measurement is nullable and means "not measured", never zero:
 * `peak_rss_bytes` is unset for runs below the executor's 60s sampling
 * floor, which is most short jobs.
 */
export interface JobRun {
  finished_at: string | null;
  outcome: string;
  duration_ms: number;
  input_bytes: number;
  peak_rss_bytes: number | null;
  threads: number | null;
  tool: string | null;
  tool_version: string | null;
  job_id: string | null;
  object_id: string | null;
}

/**
 * Recent runs grouped by job type, from GET /jobs/metrics/runs.
 *
 * `total` is the type's whole history (failures included) while `runs` is
 * only the recent window, so the UI can offer "see more" without a second
 * request.
 */
export interface RecentRuns {
  by_type: Record<string, { runs: JobRun[]; total: number }>;
}

/** One job type's runs, paged, from GET /jobs/metrics/runs?job_type=. */
export interface JobTypeRuns {
  job_type: string;
  total: number;
  runs: JobRun[];
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

/**
 * What `/jobs/types` reports for one handler.
 *
 * `default_class` is the handler's declared class, which is what tells a
 * maintenance sweep apart from the user's own work -- a job's runtime
 * `job_class` does not, since `scheduler.run_now` promotes a hand-fired sweep
 * to `user_interactive`. See `isMaintenance` in lib/runFormat.
 */
export interface JobTypeInfo {
  mode?: string;
  default_class?: JobClass;
  resources?: Record<string, unknown>;
}
