import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { formatDate } from "../lib/format";
import { notify } from "../stores/messageStore";
import type { RunMemberJob, RunSummary } from "../api/types";
import { ROLE_LABELS, STATUS_LABELS, describeParams } from "../lib/runFormat";

/**
 * One user action -- "align these reads against this reference" -- and the jobs
 * that carried it out.
 *
 * Collapsed by default. A single alignment produces seven jobs, and listing
 * them all is the noise this exists to remove; the expansion is there for when
 * something went wrong and the individual steps matter.
 */
export function RunRow({
  run,
  onSelect,
}: {
  run: RunSummary;
  onSelect?: (objectId: string, projectId: string) => void;
}) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);

  const active = run.status === "running" || run.status === "waiting";

  const { data: detail } = useQuery({
    queryKey: ["runs", run.id],
    queryFn: () => api.getRun(run.id),
    // Only fetched once expanded, or while the run is in flight and its
    // progress is worth following.
    enabled: open || active,
    refetchInterval: active ? 2000 : false,
  });

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
  const ingests = jobs.filter((j) => j.role === "ingest");
  const steps = jobs.filter((j) => j.role !== "ingest");

  const reference = run.inputs.find((i) => i.role === "reference");
  const reads = run.inputs.filter((i) => i.role === "reads" || i.role === "mate");

  return (
    <div className={`run-row ${run.status}`}>
      <div className="run-head">
        <button
          type="button"
          className="run-toggle"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          title={open ? "Hide steps" : "Show steps"}
        >
          <span className="run-chevron">{open ? "▾" : "▸"}</span>
          <span className="run-label">{run.label}</span>
        </button>

        <span className={`run-status ${run.status}`}>{STATUS_LABELS[run.status]}</span>

        {jobs.length > 0 && (
          <span className="run-count">
            {jobs.length} {jobs.length === 1 ? "job" : "jobs"}
          </span>
        )}

        {active && (
          <button
            type="button"
            className="btn run-cancel"
            onClick={() => cancel.mutate()}
            disabled={cancel.isPending}
          >
            Cancel
          </button>
        )}

        <span className="run-time">{formatDate(run.created_at)}</span>
      </div>

      {open && (
        <div className="run-detail">
          <dl className="run-facts">
            {reads.length > 0 && (
              <>
                <dt>Reads</dt>
                <dd>
                  {reads.map((i) => (
                    <button
                      key={i.object_id}
                      type="button"
                      className="run-input-link"
                      onClick={() => onSelect?.(i.object_id, run.project_id)}
                    >
                      {i.name}
                    </button>
                  ))}
                </dd>
              </>
            )}
            {reference && (
              <>
                <dt>Reference</dt>
                <dd>
                  <button
                    type="button"
                    className="run-input-link"
                    onClick={() => onSelect?.(reference.object_id, run.project_id)}
                  >
                    {reference.name}
                  </button>
                </dd>
              </>
            )}
            {Object.entries(describeParams(run.params)).map(([k, v]) => (
              <div key={k} style={{ display: "contents" }}>
                <dt>{k}</dt>
                <dd>{v}</dd>
              </div>
            ))}
            {run.outputs.length > 0 && (
              <>
                <dt>Produced</dt>
                <dd>
                  {run.outputs.length} {run.outputs.length === 1 ? "file" : "files"}
                </dd>
              </>
            )}
          </dl>

          <div className="run-steps">
            {steps.map((job) => (
              <StepRow key={job.job_id} job={job} />
            ))}
            {ingests.length > 0 && <IngestSummary jobs={ingests} />}
          </div>
        </div>
      )}
    </div>
  );
}

function StepRow({ job }: { job: RunMemberJob }) {
  const label = ROLE_LABELS[job.role] ?? job.role;
  // A pruned job has no state to show. Saying so beats inventing one -- the
  // run record deliberately outlives its jobs.
  const state = job.state ?? "expired";
  const pct =
    job.state === "running" && job.progress?.pct
      ? ` ${Math.round(job.progress.pct * 100)}%`
      : "";

  return (
    <div className="run-step">
      <span className={`run-step-state ${state}`}>{state}</span>
      <span className="run-step-label">
        {label}
        {pct}
      </span>
      {job.shared && (
        <span
          className="run-step-shared"
          title="Reused from an earlier run — this run did not do this work"
        >
          reused
        </span>
      )}
      {job.error && <span className="run-step-error">{job.error.message}</span>}
    </div>
  );
}

/**
 * Ingest jobs folded into one line.
 *
 * There is one per produced file -- four for a typical alignment -- and listing
 * them individually would reproduce the noise the grouping exists to remove.
 */
function IngestSummary({ jobs }: { jobs: RunMemberJob[] }) {
  const [open, setOpen] = useState(false);
  const failed = jobs.filter(
    (j) => j.state === "failed" || j.state === "dead" || j.state === "cancelled",
  );
  const done = jobs.filter((j) => j.state === "succeeded" || j.state === null);

  return (
    <div className="run-step run-step-ingest">
      <button type="button" className="run-ingest-toggle" onClick={() => setOpen((o) => !o)}>
        <span className="run-chevron">{open ? "▾" : "▸"}</span>
        {done.length === jobs.length
          ? `${jobs.length} ${jobs.length === 1 ? "file" : "files"} ingested`
          : `${done.length}/${jobs.length} files ingested`}
        {failed.length > 0 && (
          <span className="run-step-error"> — {failed.length} failed</span>
        )}
      </button>
      {open && (
        <div className="run-ingest-list">
          {jobs.map((j) => (
            <div key={j.job_id} className="run-step">
              <span className={`run-step-state ${j.state ?? "expired"}`}>
                {j.state ?? "expired"}
              </span>
              <span className="run-step-label">Read headers</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
