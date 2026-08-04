import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { formatDate, formatDuration } from "../lib/format";
import { RUNNING, isInFlight, jobLabel, waitingReason } from "../lib/runFormat";
import type { JobSummary, SystemLoad } from "../api/types";

/** How many finished jobs the feed carries. */
const RECENT_LIMIT = 15;

/**
 * What the machine is doing, on a phone.
 *
 * Flat rather than grouped by run, which is the whole reason this is cheap:
 * the desktop view fans `useQueries` out across every run to learn job
 * membership, up to 50 parallel requests. This asks for the job list and the
 * governor's state, and nothing else.
 *
 * Read-only in the strict sense -- nothing here is tappable. Cancelling,
 * retrying and log-reading all stay on the desktop.
 */
export function MobileActivity() {
  const qc = useQueryClient();

  const { data: jobs = [], isLoading } = useQuery({
    queryKey: ["jobs", "mobile"],
    queryFn: () => api.listJobs({ limit: 50 }),
    // Poll only while something is in flight, matching the desktop rule, so
    // a phone sitting on a finished pipeline makes no requests at all.
    refetchInterval: (q) => {
      const list = q.state.data as JobSummary[] | undefined;
      return list?.some((j) => isInFlight(j.state)) ? 2000 : false;
    },
  });

  // A job does not carry its own waiting reason -- it is derived by checking
  // the job's class against the governor's admitted_classes. Without this,
  // waitingReason degrades to a bare "waiting", which is the uninformative
  // state the feed exists to avoid.
  const { data: load } = useQuery({
    queryKey: ["systemLoad", "mobile"],
    queryFn: api.systemLoad,
    // A function, matching the jobs query, so this stops in the same tick
    // rather than lagging one interval behind a render. SystemLoad carries
    // no job-state itself, so this reads the jobs query's own live cache
    // rather than closing over the jobs variable, which would only be
    // fresh as of the last render.
    refetchInterval: () => {
      const list = qc.getQueryData<JobSummary[]>(["jobs", "mobile"]);
      return list?.some((j) => isInFlight(j.state)) ? 2000 : false;
    },
  });

  const active = jobs.filter((j) => isInFlight(j.state));
  const recent = jobs
    .filter((j) => !isInFlight(j.state))
    .slice(0, RECENT_LIMIT);

  if (isLoading) return <div className="m-empty">Loading…</div>;

  return (
    <>
      <div className="m-section-head">
        <span>In progress</span>
        <span>{active.length}</span>
      </div>
      {active.length === 0 ? (
        <div className="m-empty">Nothing running.</div>
      ) : (
        active.map((job) => <ActiveRow key={job.id} job={job} load={load} />)
      )}

      <div className="m-section-head">
        <span>Recent</span>
      </div>
      {recent.length === 0 ? (
        <div className="m-empty">No finished jobs yet.</div>
      ) : (
        recent.map((job) => <RecentRow key={job.id} job={job} />)
      )}
    </>
  );
}

function ActiveRow({ job, load }: { job: JobSummary; load?: SystemLoad }) {
  const running = RUNNING.has(job.state);
  const pct = job.progress?.pct ?? 0;

  return (
    <div className="m-row">
      <div className="m-row-title">{jobLabel(job)}</div>
      <div className="m-row-sub">
        {job.type}
        {running
          ? job.progress?.message
            ? ` · ${job.progress.message}`
            : " · running"
          : ` · ${waitingReason(job, load)}`}
      </div>
      {running && pct > 0 && (
        <div className="m-progress">
          <div style={{ width: `${Math.min(pct, 100)}%` }} />
        </div>
      )}
    </div>
  );
}

function RecentRow({ job }: { job: JobSummary }) {
  const ok = job.state === "succeeded";
  const took =
    job.timing?.duration_ms != null
      ? ` · ${formatDuration(job.timing.duration_ms)}`
      : "";

  return (
    <div className="m-row">
      <div className="m-row-title">
        {jobLabel(job)} {ok ? "✓" : "✗"}
      </div>
      <div className="m-row-sub">
        {job.type} · {formatDate(job.created_at)}
        {took}
      </div>
      {job.error && (
        <div className="m-row-error">{job.error.message}</div>
      )}
    </div>
  );
}
