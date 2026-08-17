import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import type {
  BlockedReason,
  JobState,
  RunDetail,
  RunMemberJob,
  RunSummary,
  SystemLoad,
  WorkflowRunRow,
} from "../../api/types";
import { formatClock } from "../../lib/format";
import { notify } from "../../stores/messageStore";
import {
  BLOCKED,
  ROLE_LABELS,
  STATUS_LABELS,
  WAITING,
  isUnsatisfiable,
  kindAction,
  runFacts,
  unsatisfiableReason,
  waitingReason,
} from "../../lib/runFormat";
import { LedgerRow } from "./RunLedger";
import { SectionHead } from "./SectionHead";
import { WorkflowLedgerRow } from "./WorkflowRuns";

/** States a job will not leave on its own. A pruned job (null) counts as done:
 *  the run record outlives its jobs, and a finished run whose jobs have aged
 *  out must not read as permanently half-complete. */
const SETTLED = new Set<JobState>(["succeeded", "failed", "dead", "cancelled"]);

const isSettled = (j: RunMemberJob) => j.state === null || SETTLED.has(j.state);

/**
 * What the system is doing right now, set as the page's lead story.
 *
 * One run gets the headline. A second concurrent run is possible but rare --
 * the governor serialises most pipeline work -- so the overflow is set as
 * ledger lines rather than being given equal weight and halving the space the
 * usual single run gets.
 */
export function ActivityLead({
  runs,
  workflows = [],
  details,
  load,
  onSelect,
}: {
  runs: RunSummary[];
  /** Active workflow runs, set as ledger lines under the lead story (#93).
   *  They are not candidates for the headline: a workflow's own progress lives
   *  in its node list, and the lead story is built around a run's jobs. */
  workflows?: WorkflowRunRow[];
  details: Map<string, RunDetail>;
  /** Drives each waiting step's reason. Optional: the card renders before
   *  the first /system/load response arrives. */
  load?: SystemLoad;
  onSelect: (objectId: string, projectId: string) => void;
}) {
  // One open at a time across both kinds -- the ids share a namespace here
  // only in the sense that a workflow id and a run id never collide.
  const [open, setOpen] = useState<string | null>(null);

  const [lead, ...rest] = runs;
  const jobCount = runs.reduce(
    (n, r) => n + (details.get(r.id)?.jobs.length ?? 0),
    0,
  );

  const total = runs.length + workflows.length;
  const note =
    total === 0
      ? undefined
      : `${total} ${total === 1 ? "run" : "runs"} · ${jobCount} ${
          jobCount === 1 ? "job" : "jobs"
        }`;

  return (
    <section className="activity-lead">
      <SectionHead title="In progress" note={note} />

      {!lead && workflows.length === 0 ? (
        <div className="activity-empty">Nothing running.</div>
      ) : (
        lead && (
          <LeadStory
            key={lead.id}
            run={lead}
            detail={details.get(lead.id)}
            load={load}
            onSelect={onSelect}
          />
        )
      )}

      {rest.map((run, i) => (
        <LedgerRow
          key={run.id}
          run={run}
          index={i + 2}
          jobs={details.get(run.id)?.jobs}
          open={open === run.id}
          onToggle={() => setOpen((o) => (o === run.id ? null : run.id))}
          onSelect={onSelect}
        />
      ))}

      {/* Numbered continuing from the plain runs, so a mixed column reads as
          one list. The lead story occupies 01 when there is one. */}
      {workflows.map((run, i) => (
        <WorkflowLedgerRow
          key={run.id}
          run={run}
          index={runs.length + i + 1}
          open={open === run.id}
          onToggle={() => setOpen((o) => (o === run.id ? null : run.id))}
        />
      ))}
    </section>
  );
}

function LeadStory({
  run,
  detail,
  load,
  onSelect,
}: {
  run: RunSummary;
  detail?: RunDetail;
  load?: SystemLoad;
  onSelect: (objectId: string, projectId: string) => void;
}) {
  const qc = useQueryClient();

  const cancel = useMutation({
    mutationFn: () => api.cancelRun(run.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["runs"] });
      qc.invalidateQueries({ queryKey: ["jobs"] });
      notify.info("Cancelling the run");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const jobs = detail?.jobs ?? [];
  const done = jobs.filter(isSettled).length;
  // A settled job counts as one whole unit; a running job contributes its own
  // fractional progress (0 when indeterminate) rather than nothing. Without
  // this, a run with a single long job -- most SRA downloads -- shows 0% for
  // its entire duration and jumps straight to 100%, even though the job's own
  // step line already has a real percentage to show.
  const runningCredit = jobs
    .filter((j) => j.state === "running")
    .reduce((sum, j) => sum + (j.progress?.pct ?? 0), 0);
  const pct = jobs.length > 0 ? ((done + runningCredit) / jobs.length) * 100 : 0;

  const steps = jobs.filter((j) => j.role !== "ingest");
  const ingests = jobs.filter((j) => j.role === "ingest");
  const facts = runFacts(run);
  const action = kindAction(run.kind);

  // The recorded reason describes the head of the queue, so it is shown on
  // this run's first waiting step only -- pinning it to every waiting step
  // would attribute one job's gate to all of them.
  const firstWaitingId = steps.find(
    (j) => j.state !== null && WAITING.has(j.state),
  )?.job_id;

  return (
    <>
      <div className="lead-kicker">
        {STATUS_LABELS[run.status]} · started {formatClock(run.created_at)}
      </div>

      {/* What is being done, above what it is being done to. The stored label
          names the operands ("reads → reference") and never the verb, so
          without this the biggest text on the page does not say which action
          is running. */}
      {action && <div className="lead-action">{action}</div>}

      <h2 className="lead-headline">{run.label}</h2>

      <div className="lead-progress">
        {/* Dimmed rather than hidden before the run's jobs are known: the track
            still shows there is something to measure. */}
        <div className="lead-track" style={{ opacity: jobs.length > 0 ? 1 : 0.4 }}>
          <div className="lead-fill" style={{ width: `${pct}%` }} />
        </div>
        <span className="lead-count">
          {jobs.length > 0
            ? `${done} of ${jobs.length} ${jobs.length === 1 ? "job" : "jobs"}`
            : "starting"}
        </span>
        <button
          type="button"
          className="btn"
          onClick={() => cancel.mutate()}
          disabled={cancel.isPending}
        >
          Cancel run
        </button>
      </div>

      {/* The run is already launched, so the Band.BLOCK refusal card that
          offers "Launch anyway" is behind us. Cancelling and relaunching
          with the override is the actual way out, so say that rather than
          leaving the user watching a job that cannot start. */}
      {steps.some(
        (j) =>
          j.state !== null &&
          WAITING.has(j.state) &&
          isUnsatisfiable(j.resources, load),
      ) && (
        <div className="lead-stuck-note">
          This needs more memory than this machine allows. Cancel the run and
          relaunch it — the launch dialog offers "Launch anyway", or lower the
          thread count to reduce what it needs.
        </div>
      )}

      <div className="lead-facts">
        {facts.map((f) => (
          <div key={f.k} className="activity-fact">
            <span className="activity-fact-k">{f.k}</span>
            <span className="activity-fact-v">{f.v}</span>
          </div>
        ))}
      </div>

      {run.inputs.length > 0 && (
        <div className="lead-links">
          {run.inputs.map((i) => (
            <button
              key={`${i.object_id}-${i.role}`}
              type="button"
              className="run-input-link"
              onClick={() => onSelect(i.object_id, run.project_id)}
            >
              {i.name}
            </button>
          ))}
        </div>
      )}

      <div className="lead-steps">
        {steps.map((job) => (
          <LeadStep
            key={job.job_id}
            job={job}
            load={load}
            reason={job.job_id === firstWaitingId ? load?.blocked_reason : null}
          />
        ))}
        {ingests.length > 0 && <IngestStep jobs={ingests} />}
      </div>
    </>
  );
}

function LeadStep({
  job,
  load,
  reason,
}: {
  job: RunMemberJob;
  load?: SystemLoad;
  reason?: BlockedReason | null;
}) {
  // A pruned job has no state to show. Saying so beats inventing one.
  const state = job.state ?? "expired";
  const pct =
    job.state === "running" && job.progress?.pct
      ? ` ${Math.round(job.progress.pct * 100)}%`
      : "";

  // #457: a run-owned job showed a bare "queued" with nothing saying what it
  // was queued behind. waitingReason already answered this for loose jobs in
  // the "Other waiting" section; this is the same sentence on the card users
  // actually watch.
  const isWaiting =
    job.state !== null && (WAITING.has(job.state) || BLOCKED.has(job.state));
  // A job demanding more than the whole budget is not waiting its turn --
  // nothing that finishes will free enough. It gets its own words and its
  // own colour, and it is checked first because the queue would otherwise
  // report it as an ordinary memory wait forever (#457).
  const stuck = isWaiting && isUnsatisfiable(job.resources, load);
  const why = !isWaiting
    ? null
    : stuck && job.resources && load?.memory.budget_bytes != null
      ? unsatisfiableReason(job.resources, load.memory.budget_bytes)
      : waitingReason(
          {
            state: job.state as string,
            job_class: job.job_class ?? "",
            cancel_requested: job.cancel_requested,
          },
          load,
          reason,
        );

  return (
    <div className="lead-step">
      <span className={`lead-step-state ${state}`}>{state}</span>
      <span className="lead-step-label">
        {ROLE_LABELS[job.role] ?? job.role}
        {pct}
        {job.shared && (
          <span
            className="lead-step-shared"
            title="Reused from an earlier run — this run did not do this work"
          >
            reused
          </span>
        )}
      </span>
      {why && (
        <span className={stuck ? "lead-step-stuck" : "lead-step-why"}>{why}</span>
      )}
      {job.error && <span className="lead-step-error">{job.error.message}</span>}
      <span className="lead-step-time">{formatClock(job.created_at)}</span>
    </div>
  );
}

/**
 * Every ingest job on one line.
 *
 * There is one per produced file -- four for a typical alignment -- and the
 * lead column has no room to list them. Unlike the expandable version in
 * `RunRow`, this does not open: the individual ingests differ only by which
 * file they read, and that is already the "Produced" fact above.
 */
function IngestStep({ jobs }: { jobs: RunMemberJob[] }) {
  const done = jobs.filter(isSettled);
  const failed = jobs.filter(
    (j) => j.state === "failed" || j.state === "dead" || j.state === "cancelled",
  );
  const complete = done.length === jobs.length;

  return (
    <div className="lead-step">
      <span
        className={`lead-step-state ${
          failed.length > 0 ? "failed" : complete ? "succeeded" : "running"
        }`}
      >
        {failed.length > 0 ? "failed" : complete ? "ingested" : "ingesting"}
      </span>
      <span className="lead-step-label">
        {complete
          ? `${jobs.length} ${jobs.length === 1 ? "file" : "files"} ingested`
          : `${done.length} / ${jobs.length} files ingested`}
        {failed.length > 0 && (
          <span className="lead-step-error"> — {failed.length} failed</span>
        )}
      </span>
      <span className="lead-step-time">
        {complete ? formatClock(jobs[jobs.length - 1]?.created_at) : "—"}
      </span>
    </div>
  );
}
