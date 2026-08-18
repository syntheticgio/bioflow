import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { formatBytes, formatDate, formatDuration } from "../lib/format";
import { notify } from "../stores/messageStore";
import type { JobSummary } from "../api/types";

const ACTIVE_STATES = new Set(["pending", "queued", "delayed", "running"]);

export function JobList({ projectId, limit = 10 }: { projectId?: string; limit?: number }) {
  const qc = useQueryClient();

  const { data: jobs } = useQuery({
    queryKey: ["jobs", projectId ?? "all"],
    queryFn: () => api.listJobs({ projectId, limit }),
    refetchInterval: (q) => {
      const list = q.state.data as JobSummary[] | undefined;
      // Poll only while something is in flight; SSE covers the rest.
      return list?.some((j) => ACTIVE_STATES.has(j.state)) ? 2000 : false;
    },
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

  if (!jobs || jobs.length === 0) {
    return <div style={{ color: "var(--text-faint)", fontSize: 13 }}>No jobs yet.</div>;
  }

  return (
    <div>
      {jobs.map((job) => {
        const active = ACTIVE_STATES.has(job.state);
        const { pct } = job.progress;
        const indeterminate = pct === null;

        return (
          <div
            key={job.id}
            style={{
              padding: "8px 0",
              borderBottom: "1px solid var(--border)",
              fontSize: 13,
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <span className="mono">{job.type}</span>
              <span className={`badge ${job.state}`}>{job.state}</span>
              <span style={{ color: "var(--text-faint)", fontSize: 11 }}>
                {job.job_class}
                {job.progress.phase && ` · ${job.progress.phase}`}
                {job.progress.phase_index != null && job.progress.phase_total != null && (
                  ` (step ${job.progress.phase_index}/${job.progress.phase_total})`
                )}
              </span>
              <span style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 6 }}>
                {active && (
                  <button
                    type="button"
                    className="btn"
                    style={{ padding: "2px 8px", fontSize: 12 }}
                    onClick={() => cancel.mutate(job.id)}
                    disabled={job.cancel_requested}
                  >
                    {job.cancel_requested ? "Cancelling…" : "Cancel"}
                  </button>
                )}
                {(job.state === "failed" || job.state === "dead") && (
                  <button
                    type="button"
                    className="btn"
                    style={{ padding: "2px 8px", fontSize: 12 }}
                    onClick={() => retry.mutate(job.id)}
                  >
                    Retry
                  </button>
                )}
                <span style={{ color: "var(--text-faint)", fontSize: 11 }}>
                  {formatDate(job.created_at)}
                </span>
              </span>
            </div>

            {job.state === "running" && (
              <div className="progress" style={{ marginTop: 5 }}>
                <div
                  className={`progress-bar${indeterminate ? " indeterminate" : ""}${job.progress.pct_estimated != null ? " estimated" : ""}`}
                  style={indeterminate ? undefined : { width: `${Math.round((job.progress.pct_estimated ?? pct) * 100)}%` }}
                />
              </div>
            )}

            {job.state === "running" &&
              job.progress.units_done != null &&
              job.progress.units_total != null && (
                <div style={{ color: "var(--text-faint)", fontSize: 11, marginTop: 3 }}>
                  {job.progress.units_done.toLocaleString()} /{" "}
                  {job.progress.units_total.toLocaleString()}
                  {job.progress.unit_label && ` ${job.progress.unit_label}`}
                </div>
              )}

            {job.state === "running" && job.progress.eta_seconds != null && (
              <div style={{ color: "var(--text-faint)", fontSize: 11, marginTop: 3 }}>
                ~{formatDuration(job.progress.eta_seconds * 1000)} remaining
                {job.progress.pct_estimated != null && " (estimated)"}
              </div>
            )}

            {job.state === "running" &&
              (job.progress.rss_bytes != null || job.progress.cpu_percent != null) && (
                <div style={{ color: "var(--text-faint)", fontSize: 11, marginTop: 3 }}>
                  {job.progress.rss_bytes != null && formatBytes(job.progress.rss_bytes)}
                  {job.progress.rss_bytes != null && job.progress.cpu_percent != null && " · "}
                  {job.progress.cpu_percent != null &&
                    `${job.progress.cpu_percent.toFixed(0)}% CPU`}
                  {job.progress.peak_rss_bytes != null &&
                    `, peaking at ${formatBytes(job.progress.peak_rss_bytes)}`}
                </div>
              )}

            {job.attempts > 1 && (
              <div style={{ color: "var(--text-faint)", fontSize: 11, marginTop: 3 }}>
                attempt {job.attempts}/{job.max_attempts}
                {job.last_attempt_progress && (
                  <>
                    {" — attempt "}
                    {job.last_attempt_progress.attempt} reached{" "}
                    {job.last_attempt_progress.pct !== null
                      ? `${Math.round(job.last_attempt_progress.pct * 100)}%`
                      : job.last_attempt_progress.phase || "no progress"}
                    {job.last_attempt_progress.peak_rss_bytes != null &&
                      `, peaking at ${formatBytes(job.last_attempt_progress.peak_rss_bytes)}`}
                  </>
                )}
              </div>
            )}

            {job.error && (
              <div style={{ color: "var(--error)", fontSize: 11, marginTop: 3 }}>
                {job.error.code}: {job.error.message}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
