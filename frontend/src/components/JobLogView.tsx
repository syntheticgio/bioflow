import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";

/**
 * Captured output from a job that shelled out to a tool.
 *
 * Only the tail is fetched. A fastp run over a large library writes far more
 * than anyone reads, and the interesting part -- what it detected, where it
 * failed -- is always at the end.
 */
export function JobLogView({ jobId, live = false }: { jobId: string; live?: boolean }) {
  const { data, isLoading } = useQuery({
    queryKey: ["job", jobId, "log"],
    queryFn: () => api.getJobLog(jobId, 200),
    refetchInterval: live ? 3000 : false,
  });

  if (isLoading) {
    return <div className="job-log job-log-empty">Loading…</div>;
  }

  if (!data?.exists) {
    return (
      <div className="job-log job-log-empty">
        No log — this job has not written any output yet.
      </div>
    );
  }

  if (data.lines.length === 0) {
    return <div className="job-log job-log-empty">Log is empty.</div>;
  }

  return (
    <div className="job-log">
      {data.truncated && (
        <div className="job-log-note">Showing the last {data.lines.length} lines.</div>
      )}
      <pre>{data.lines.join("\n")}</pre>
    </div>
  );
}
