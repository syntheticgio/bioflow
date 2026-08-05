import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { formatDuration } from "../lib/format";
import type { JobSummary } from "../api/types";

const RUNNING = new Set(["running"]);
const WAITING = new Set(["pending", "queued", "delayed"]);

/**
 * Enough of the queue to answer "is it done yet?" without leaving the file
 * you are looking at.
 *
 * Capped at a handful of rows: a maintenance backlog would otherwise make the
 * panel taller than the window and bury the running job that prompted opening
 * it. The overflow count links to the full activity view rather than scrolling
 * here, which is what that page is for.
 */
const MAX_ROWS = 10;

export function QueuePanel({ onClose }: { onClose: () => void }) {
  const { data: jobs = [] } = useQuery({
    queryKey: ["jobs", "queue-panel"],
    queryFn: () => api.listJobs({ states: "active", limit: 100 }),
    refetchInterval: 2000,
  });

  const running = jobs.filter((j) => RUNNING.has(j.state));
  const waiting = jobs.filter((j) => WAITING.has(j.state));

  // Running first: it is what someone opening this panel is asking about.
  const shown = [...running, ...waiting].slice(0, MAX_ROWS);
  const hidden = running.length + waiting.length - shown.length;

  return (
    <>
      {/* Click-away, matching the modal pattern used elsewhere. */}
      <div className="queue-backdrop" onClick={onClose} />
      <div className="queue-panel">
        <div className="queue-panel-head">
          <span className="panel-title">Queue</span>
          <span className="queue-panel-counts">
            {running.length} running · {waiting.length} waiting
          </span>
          <button
            type="button"
            className="icon-btn"
            onClick={onClose}
            title="Close"
            style={{ marginLeft: "auto" }}
          >
            ×
          </button>
        </div>

        {shown.length === 0 ? (
          <div className="queue-empty">Nothing running or queued.</div>
        ) : (
          <div className="queue-rows">
            {shown.map((job) => (
              <QueueRow key={job.id} job={job} />
            ))}
          </div>
        )}

        <div className="queue-panel-foot">
          {hidden > 0 && <span>{hidden} more</span>}
          <Link to="/activity" onClick={onClose} style={{ marginLeft: "auto" }}>
            Open activity →
          </Link>
        </div>
      </div>
    </>
  );
}

function QueueRow({ job }: { job: JobSummary }) {
  const isRunning = RUNNING.has(job.state);
  const { pct } = job.progress;
  const indeterminate = pct === null;
  const started = job.timing.started_at;
  const elapsed = started ? Date.now() - new Date(started).getTime() : null;

  return (
    <div className="queue-row">
      <div className="queue-row-head">
        <span className="queue-row-name">{jobLabel(job)}</span>
        <span className={`badge ${job.state}`}>{job.state}</span>
      </div>

      {isRunning && (
        <div
          className="progress"
          style={{ marginTop: 4, opacity: indeterminate || pct > 0 ? 1 : 0.25 }}
        >
          <div
            className={`progress-bar${indeterminate ? " indeterminate" : ""}`}
            style={indeterminate ? undefined : { width: `${Math.round(pct * 100)}%` }}
          />
        </div>
      )}

      <div className="queue-row-meta">
        <span className="mono">{job.type}</span>
        {job.progress.message && <span>{job.progress.message}</span>}
        {isRunning && elapsed !== null && <span>{formatDuration(elapsed)}</span>}
      </div>
    </div>
  );
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
