import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { useState } from "react";
import { api } from "../api/client";
import { formatDate, formatDuration } from "../lib/format";
import { notify } from "../stores/messageStore";
import type { JobSummary, RunDetail, RunSummary, SystemLoad } from "../api/types";
import { JobLogView } from "./JobLogView";
import { ActivityDesk } from "./activity/ActivityDesk";
import { ActivityLead } from "./activity/ActivityLead";
import { RunLedger } from "./activity/RunLedger";

const RUNNING = new Set(["running"]);
const WAITING = new Set(["pending", "queued", "delayed"]);

/** How many finished runs the ledger column carries. */
const LEDGER_LIMIT = 10;

/**
 * Everything the system is doing, and why it is not doing the rest.
 *
 * The "why" matters more than it looks. A queued job with no explanation is
 * indistinguishable from a stuck one, and the load governor deliberately
 * defers work -- so the honest answer is usually "the machine is busy", not
 * "something is wrong".
 *
 * Laid out as three columns -- the run in progress, the ledger of recent ones,
 * and a standing rail of library figures -- over a full-width strip of the work
 * that belongs to no run. The strip is the part that answers the "why": the
 * governor note and each job's waiting reason live there.
 */
export function ActivityView() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [openLog, setOpenLog] = useState<string | null>(null);

  const { data: jobs = [] } = useQuery({
    queryKey: ["jobs", "activity"],
    queryFn: () => api.listJobs({ limit: 200 }),
    // Poll only while something is in flight; SSE covers the rest.
    refetchInterval: (q) => {
      const list = q.state.data as JobSummary[] | undefined;
      return list?.some((j) => RUNNING.has(j.state) || WAITING.has(j.state))
        ? 2000
        : false;
    },
  });

  const { data: runs = [] } = useQuery({
    queryKey: ["runs", "activity"],
    queryFn: () => api.listRuns({ limit: 50 }),
    refetchInterval: (q) => {
      const list = q.state.data as RunSummary[] | undefined;
      return list?.some((r) => r.status === "running" || r.status === "waiting")
        ? 2000
        : false;
    },
  });

  // Membership, so a job shown inside its run is not also listed loose. Every
  // run is fetched rather than only the expanded ones -- the collapsed rows
  // still need to know which jobs they own, and the columns show a job count
  // before anything is expanded.
  const runDetails = useQueries({
    queries: runs.map((run) => ({
      queryKey: ["runs", run.id],
      queryFn: () => api.getRun(run.id),
      refetchInterval:
        run.status === "running" || run.status === "waiting" ? 2000 : false,
    })),
  }).map((q) => q.data);

  const details = new Map<string, RunDetail>();
  for (const detail of runDetails) if (detail) details.set(detail.id, detail);

  const { data: load } = useQuery({
    queryKey: ["system", "load"],
    queryFn: api.systemLoad,
    refetchInterval: 5000,
  });

  const cancel = useMutation({
    mutationFn: (id: string) => api.cancelJob(id),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      notify.info(
        res.outcome === "cancelling"
          ? "Cancelling — the job will stop at its next checkpoint"
          : "Job cancelled",
      );
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const retry = useMutation({
    mutationFn: (id: string) => api.retryJob(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      notify.success("Job requeued");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  // Jobs that belong to a run are shown inside it, not loose. The set is built
  // from the runs rather than from a flag on the job, so a job whose run has
  // been deleted falls back to being listed individually rather than vanishing.
  const grouped = new Set<string>();
  for (const detail of runDetails) {
    for (const job of detail?.jobs ?? []) grouped.add(job.job_id);
  }

  const loose = jobs.filter((j) => !grouped.has(j.id));
  const running = loose.filter((j) => RUNNING.has(j.state));
  const waiting = loose.filter((j) => WAITING.has(j.state));
  const recent = loose.filter(
    (j) => !RUNNING.has(j.state) && !WAITING.has(j.state),
  );

  const activeRuns = runs.filter(
    (r) => r.status === "running" || r.status === "waiting",
  );
  const finishedRuns = runs.filter(
    (r) => r.status !== "running" && r.status !== "waiting",
  );

  const selectObject = (objectId: string, projectId: string) =>
    navigate(`/p/${projectId}?sel=object:${objectId}`);

  // Navigates rather than just setting the selection: the detail panel lives
  // in the explorer, and this view deliberately does not render beside it.
  const select = (job: JobSummary) => {
    if (!job.object_id) return;
    const project = job.project_id ? `/p/${job.project_id}` : "/";
    navigate(`${project}?sel=object:${job.object_id}`);
  };

  return (
    <div className="panel activity">
      <div className="panel-header">
        <span className="panel-title">Activity</span>
        {load && <GovernorNote load={load} waiting={waiting.length} />}
      </div>

      {/* Runs first: one column per action the user took, rather than the seven
          jobs an alignment decomposes into. */}
      <div className="activity-grid">
        <ActivityLead
          runs={activeRuns}
          details={details}
          onSelect={selectObject}
        />

        <RunLedger
          runs={finishedRuns.slice(0, LEDGER_LIMIT)}
          jobCounts={
            new Map(
              finishedRuns.map((r) => [r.id, details.get(r.id)?.jobs.length ?? 0]),
            )
          }
          onSelect={selectObject}
        />

        <ActivityDesk />
      </div>

      {/* Everything below is work that belongs to no run: verification
          sweeps, reaping, a manual re-ingest. Full width under the columns --
          it is diagnostic rather than a headline, but it is the only place a
          dead job can be retried or a trim's log read. */}
      <div className="activity-loose">
        <Section title="Other running" count={running.length} empty="Nothing running.">
          {running.map((job) => (
            <JobRow
              key={job.id}
              job={job}
              onSelect={select}
              onCancel={() => cancel.mutate(job.id)}
              onToggleLog={() => setOpenLog(openLog === job.id ? null : job.id)}
              logOpen={openLog === job.id}
            />
          ))}
        </Section>

        <Section
          title="Other waiting"
          count={waiting.length}
          empty="Nothing queued."
        >
          {waiting.map((job) => (
            <JobRow
              key={job.id}
              job={job}
              load={load}
              onSelect={select}
              onCancel={() => cancel.mutate(job.id)}
            />
          ))}
        </Section>

        <Section title="Other recent" count={recent.length} empty="No finished jobs.">
          {recent.slice(0, 40).map((job) => (
            <JobRow
              key={job.id}
              job={job}
              onSelect={select}
              onRetry={
                job.state === "failed" || job.state === "dead"
                  ? () => retry.mutate(job.id)
                  : undefined
              }
              onToggleLog={() => setOpenLog(openLog === job.id ? null : job.id)}
              logOpen={openLog === job.id}
            />
          ))}
        </Section>
      </div>
    </div>
  );
}

function Section({
  title,
  count,
  empty,
  children,
}: {
  title: string;
  count: number;
  empty: string;
  children: React.ReactNode;
}) {
  return (
    <div className="section">
      <div className="section-title activity-section-title">
        <span>{title}</span>
        {count > 0 && <span className="activity-count">{count}</span>}
      </div>
      {count === 0 ? (
        <div style={{ color: "var(--text-faint)", fontSize: 13 }}>{empty}</div>
      ) : (
        children
      )}
    </div>
  );
}

/** Why queued work is not starting, when the governor is the reason. */
function GovernorNote({ load, waiting }: { load: SystemLoad; waiting: number }) {
  if (load.state === "OPEN" || waiting === 0) return null;
  const detail =
    load.state === "CLOSED"
      ? "System loaded — only interactive work is starting"
      : "System busy — background and pipeline work is deferred";
  return (
    <span style={{ marginLeft: "auto", fontSize: 11, color: "var(--warn)" }}>
      {detail}
    </span>
  );
}

function JobRow({
  job,
  load,
  onSelect,
  onCancel,
  onRetry,
  onToggleLog,
  logOpen,
}: {
  job: JobSummary;
  load?: SystemLoad;
  onSelect: (j: JobSummary) => void;
  onCancel?: () => void;
  onRetry?: () => void;
  onToggleLog?: () => void;
  logOpen?: boolean;
}) {
  const pct = Math.round((job.progress.pct ?? 0) * 100);
  const started = job.timing.started_at;
  const elapsed = started ? Date.now() - new Date(started).getTime() : null;

  // Only shown for jobs that shell out to a tool; everything else writes none.
  const mayHaveLog = job.type === "trim_reads";

  return (
    <div className="activity-row">
      <div className="activity-row-head">
        <button
          type="button"
          className="activity-name"
          onClick={() => onSelect(job)}
          title={job.object_id ? "Show this file" : undefined}
          disabled={!job.object_id}
        >
          {jobLabel(job)}
        </button>
        <span className={`badge ${job.state}`}>{job.state}</span>

        <span style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          {mayHaveLog && onToggleLog && (
            <button
              type="button"
              className="btn"
              style={{ padding: "2px 8px", fontSize: 12 }}
              onClick={onToggleLog}
            >
              {logOpen ? "Hide log" : "Log"}
            </button>
          )}
          {onRetry && (
            <button
              type="button"
              className="btn"
              style={{ padding: "2px 8px", fontSize: 12 }}
              onClick={onRetry}
            >
              Retry
            </button>
          )}
          {onCancel && (
            <button
              type="button"
              className="btn"
              style={{ padding: "2px 8px", fontSize: 12 }}
              onClick={onCancel}
              disabled={job.cancel_requested}
            >
              {job.cancel_requested ? "Cancelling…" : "Cancel"}
            </button>
          )}
        </span>
      </div>

      {job.state === "running" && (
        <div
          className="progress"
          style={{ marginTop: 5, opacity: job.progress.pct > 0 ? 1 : 0.25 }}
          title={
            job.progress.pct >= 0.95
              ? "Read counts are estimates, so the bar stops short of complete"
              : undefined
          }
        >
          <div className="progress-bar" style={{ width: `${pct || 100}%` }} />
        </div>
      )}

      <div className="activity-row-meta">
        <span className="mono">{job.type}</span>
        <span>{job.job_class}</span>
        {job.progress.message && <span>{job.progress.message}</span>}
        {job.attempts > 1 && (
          <span>
            attempt {job.attempts}/{job.max_attempts}
          </span>
        )}
        {job.state === "running" && elapsed !== null && (
          <span>{formatDuration(elapsed)} elapsed</span>
        )}
        {WAITING.has(job.state) && (
          <span>{waitingReason(job, load)}</span>
        )}
        {job.timing.duration_ms != null && (
          <span>took {formatDuration(job.timing.duration_ms)}</span>
        )}
        <span style={{ marginLeft: "auto" }}>{formatDate(job.created_at)}</span>
      </div>

      {job.error && (
        <div style={{ color: "var(--error)", fontSize: 11, marginTop: 3 }}>
          {job.error.code}: {job.error.message}
        </div>
      )}

      {logOpen && <JobLogView jobId={job.id} live={job.state === "running"} />}
    </div>
  );
}

/**
 * A spinner says "wait"; this says what for. The governor's admitted_classes
 * is authoritative about whether this job's class can start at all.
 */
function waitingReason(job: JobSummary, load?: SystemLoad): string {
  if (job.cancel_requested) return "cancelling";
  if (job.state === "delayed") return "retrying after a failure";
  if (!load) return "waiting";
  if (!load.admitted_classes.includes(job.job_class)) {
    return load.state === "CLOSED"
      ? "waiting: system loaded"
      : "waiting: system busy";
  }
  return "waiting for a free slot";
}

/** The file a job is about, falling back to its type. */
function jobLabel(job: JobSummary): string {
  const payload = job.payload as Record<string, unknown>;
  const name = payload.r1_name ?? payload.name;
  if (typeof name === "string" && name) {
    const mate = payload.r2_name;
    return typeof mate === "string" && mate ? `${name} + ${mate}` : name;
  }
  return job.type;
}
