import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { formatDate } from "../lib/format";
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
        const pct = Math.round((job.progress.pct ?? 0) * 100);

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
                <div className="progress-bar" style={{ width: `${pct}%` }} />
              </div>
            )}

            {job.attempts > 1 && (
              <div style={{ color: "var(--text-faint)", fontSize: 11, marginTop: 3 }}>
                attempt {job.attempts}/{job.max_attempts}
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
