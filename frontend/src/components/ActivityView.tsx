import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { useState } from "react";
import { api } from "../api/client";
import { assertEach, assertRunSummary } from "../api/validators";
import { formatBytes, formatDate, formatDuration } from "../lib/format";
import { notify } from "../stores/messageStore";
import type { JobSummary, RunDetail, RunSummary, SystemLoad } from "../api/types";
import { JobLogView } from "./JobLogView";
import { ActivityDesk } from "./activity/ActivityDesk";
import { ActivityLead } from "./activity/ActivityLead";
import { RunLedger } from "./activity/RunLedger";
import { SectionHead } from "./activity/SectionHead";
import { useWorkflowRuns } from "./activity/WorkflowRuns";
import { BLOCKED, RUNNING, WAITING, jobLabel } from "../lib/runFormat";

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
    queryFn: async () => {
      const list = await api.listRuns({ limit: 50 });
      assertEach(assertRunSummary, list);
      return list;
    },
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

  // Workflows are runs too, and go in the same two columns (#93) rather than
  // in a section of their own above them.
  const workflows = useWorkflowRuns();

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

      {/* Runs: one column per action the user took, rather than the seven jobs
          an alignment decomposes into. Workflows are among them -- a workflow
          is one user action that becomes several runs, so it belongs in the
          same columns rather than in a third place with its own look. */}
      <div className="activity-grid">
        <div className="activity-lead-column">
          <ActivityLead
            runs={activeRuns}
            workflows={workflows.active}
            details={details}
            load={load}
            onSelect={selectObject}
          />

          <SectionHead title="Other running" note={running.length > 0 ? String(running.length) : undefined} />
          {running.length > 0 ? (
            running.map((job) => (
              <JobRow
                key={job.id}
                job={job}
                onSelect={select}
                onCancel={() => cancel.mutate(job.id)}
                onToggleLog={() => setOpenLog(openLog === job.id ? null : job.id)}
                logOpen={openLog === job.id}
              />
            ))
          ) : (
            <div className="activity-empty">Nothing running.</div>
          )}
        </div>

        <RunLedger
          runs={finishedRuns.slice(0, LEDGER_LIMIT)}
          workflows={workflows.finished.slice(0, LEDGER_LIMIT)}
          jobsByRun={
            new Map(
              finishedRuns.map((r) => [r.id, details.get(r.id)?.jobs ?? []]),
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
        <SectionHead title="Other waiting" note={waiting.length > 0 ? String(waiting.length) : undefined} />
        {waiting.length > 0 ? (
          waiting.map((job) => (
            <JobRow
              key={job.id}
              job={job}
              load={load}
              onSelect={select}
              onCancel={() => cancel.mutate(job.id)}
            />
          ))
        ) : (
          <div className="activity-empty">Nothing queued.</div>
        )}

        <SectionHead title="Other recent" note={recent.length > 0 ? String(recent.length) : undefined} />
        {recent.length > 0 ? (
          recent.slice(0, 40).map((job) => (
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
          ))
        ) : (
          <div className="activity-empty">No finished jobs.</div>
        )}
      </div>
    </div>
  );
}

function GovernorNote({
  load,
  waiting,
}: {
  load: SystemLoad;
  waiting: number;
}) {
  const text = `${load.used_cpu_percent.toFixed(1)}% CPU · ${formatBytes(load.used_memory_bytes)} RAM`;
  return (
    <span className="governor-note" title={waiting > 0 ? `${waiting} waiting` : undefined}>
      {text}
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
  const isRunning = RUNNING.has(job.state);
  const isWaiting = WAITING.has(job.state);
  const isBlocked = BLOCKED.has(job.state);
  return (
    <div className="activity-row">
      <div className="activity-row-head">
        <span className="activity-row-label" onClick={() => onSelect(job)}>
          {jobLabel(job)}
        </span>
        <span className="activity-row-state">
          {isBlocked ? "blocked" : isRunning ? "running" : isWaiting ? "waiting" : job.state}
        </span>
        {onCancel && (isRunning || isWaiting) && (
          <button className="btn-text" onClick={onCancel}>Cancel</button>
        )}
        {onRetry && !isRunning && !isWaiting && (
          <button className="btn-text" onClick={onRetry}>Retry</button>
        )}
        {onToggleLog && (
          <button className="btn-text" onClick={onToggleLog}>
            {logOpen ? "Hide log" : "Log"}
         Monochromatic</button>
        )}
      </div>
      {logOpen && <JobLogView jobId={job.id} />}
    </div>
  );
}
