import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { JobSummary } from "../api/types";

/** Job types this indicator reports on: the ones a user launches from here. */
const PIPELINE_TYPES = new Set([
  "trim_reads",
  "align_reads",
  "build_index",
  "index_bam",
  "run_bam_stats",
]);

const LABELS: Record<string, string> = {
  trim_reads: "Trimming",
  align_reads: "Aligning",
  build_index: "Building index",
  index_bam: "Indexing BAM",
  run_bam_stats: "Computing results",
};

/**
 * A reminder that work is already in flight for this file.
 *
 * Deliberately not a guard: running a second alignment with different settings
 * is legitimate, and the dedup key already collapses an accidental double
 * submit of the *same* settings. This exists so a user who queued something a
 * minute ago and forgot is less likely to queue it again -- the buttons stay
 * enabled either way.
 */
export function ActivePipelineJobs({ objectId }: { objectId: string }) {
  const { data } = useQuery({
    queryKey: ["jobs", "for-object", objectId],
    queryFn: () => api.listJobs({ objectId, states: "active", limit: 20 }),
    // A pipeline run reports progress but publishes no event this component
    // listens to, so poll often enough that a finished run clears promptly.
    refetchInterval: 5_000,
  });

  const jobs = (data ?? []).filter((j) => PIPELINE_TYPES.has(j.type));
  if (jobs.length === 0) return null;

  return (
    <span className="active-jobs" title="Work already queued or running for this file">
      {jobs.map((job) => (
        <Link key={job.id} to="/activity" className={`active-job ${job.state}`}>
          <span className="active-job-dot" aria-hidden="true" />
          {describe(job)}
        </Link>
      ))}
    </span>
  );
}

function describe(job: JobSummary): string {
  const label = LABELS[job.type] ?? job.type;

  if (job.state === "running") {
    // pct === null means indeterminate (no honest fraction available), which
    // must not render the same as a real 0% -- collapsing the two with `?? 0`
    // was the bug here: an unstarted job and one with no measurable progress
    // looked identical. Only show a number once it is a real, positive
    // fraction; "Aligning 0%" reads as stalled when it means "just started".
    const rawPct = job.progress?.pct;
    if (rawPct != null && rawPct > 0) {
      return `${label} ${Math.round(rawPct * 100)}%`;
    }
    return label;
  }
  // Blocked is worth naming rather than folding into "queued": it means the
  // job is waiting on another one (an index build) rather than on capacity,
  // and a user who sees "queued" for minutes would reasonably wonder why.
  if (job.state === "blocked") return `${label} — waiting on index`;
  return `${label} queued`;
}
