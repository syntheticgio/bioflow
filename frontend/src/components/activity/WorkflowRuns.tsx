/**
 * Workflow runs in the activity view.
 *
 * A workflow's nodes are ordinary `PipelineRun`s and jobs, deliberately (design
 * §1.5), which is exactly why this section has to exist: without it a workflow
 * is a flat pile of unrelated rows with nothing saying they belong together or
 * how far along the whole thing is.
 *
 * Status is derived server-side from node states and shares `RunStatus`'s
 * vocabulary, so the existing STATUS_LABELS apply unchanged.
 *
 * Live updates come from polling while a run is active -- the same 2s cadence
 * the runs and jobs queries above already use. `run_ids` on `job.progress`
 * cannot serve here: it carries `PipelineRun` ids, and 13 of the 22 node types
 * create no run at all, so a workflow whose QC node is working would report
 * nothing. See the deviation note on #80.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../../api/client";
import type { WorkflowNodeRow, WorkflowRunRow } from "../../api/types";
import { formatClock } from "../../lib/format";
import { STATUS_LABELS } from "../../lib/runFormat";
import { notify } from "../../stores/messageStore";
import { SectionHead } from "./SectionHead";

const ACTIVE = new Set(["running", "waiting"]);

export function WorkflowRuns() {
  const [open, setOpen] = useState<string | null>(null);

  const { data: runs = [] } = useQuery({
    queryKey: ["workflow-runs"],
    queryFn: () => api.listWorkflowRuns(),
    // Poll only while something is in flight, matching the runs query.
    refetchInterval: (q) => {
      const list = q.state.data as WorkflowRunRow[] | undefined;
      return list?.some((r) => ACTIVE.has(r.status)) ? 2000 : false;
    },
  });

  // The section disappears entirely when no workflow has ever run, rather than
  // showing an empty box: most installs will never use workflows, and an empty
  // heading is a permanent question about a feature they are not using.
  if (runs.length === 0) return null;

  return (
    <section className="activity-workflows">
      <SectionHead title="Workflows" note={`${runs.length}`} />
      {runs.map((run) => (
        <WorkflowRow
          key={run.id}
          run={run}
          open={open === run.id}
          onToggle={() => setOpen((o) => (o === run.id ? null : run.id))}
        />
      ))}
    </section>
  );
}

function WorkflowRow({
  run,
  open,
  onToggle,
}: {
  run: WorkflowRunRow;
  open: boolean;
  onToggle: () => void;
}) {
  const qc = useQueryClient();

  const { data: detail } = useQuery({
    queryKey: ["workflow-run", run.id],
    queryFn: () => api.getWorkflowRun(run.id),
    // Fetched only when expanded: the collapsed row already carries its counts,
    // so the detail is the expensive half and nobody needs it unopened.
    enabled: open,
    refetchInterval: open && ACTIVE.has(run.status) ? 2000 : false,
  });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["workflow-runs"] });
    qc.invalidateQueries({ queryKey: ["workflow-run", run.id] });
    qc.invalidateQueries({ queryKey: ["jobs"] });
  };

  const retryNode = useMutation({
    mutationFn: (nodeId: string) => api.retryWorkflowNode(run.id, nodeId),
    onSuccess: () => {
      invalidate();
      notify.success("Node requeued");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const retryAll = useMutation({
    mutationFn: () => api.retryFailedWorkflowNodes(run.id),
    onSuccess: (res) => {
      invalidate();
      notify.success(
        res.retried === 1 ? "1 node requeued" : `${res.retried} nodes requeued`,
      );
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const cancel = useMutation({
    mutationFn: () => api.cancelWorkflowRun(run.id),
    onSuccess: () => {
      invalidate();
      notify.info("Cancelling — running nodes stop at their next checkpoint");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  return (
    <div className={`workflow-run${open ? " open" : ""}`}>
      <button className="workflow-run-head" onClick={onToggle}>
        <span className="workflow-run-chevron">{open ? "▾" : "▸"}</span>
        <span className="workflow-run-label">{run.label}</span>
        <span className={`workflow-run-status ${run.status}`}>
          {STATUS_LABELS[run.status]}
        </span>
        <span className="workflow-run-count">
          {run.node_done}/{run.node_total}
          {/* Failures are called out separately rather than folded into the
              fraction: a PARTIAL run has real outputs *and* a dead branch, and
              "2/3" alone hides which. */}
          {run.node_failed > 0 && (
            <em className="workflow-run-failed"> · {run.node_failed} failed</em>
          )}
        </span>
        <span className="workflow-run-time">{formatClock(run.created_at)}</span>
      </button>

      {open && (
        <div className="workflow-run-body">
          <div className="workflow-run-actions">
            {run.node_failed > 0 && (
              <button
                className="btn small"
                onClick={() => retryAll.mutate()}
                disabled={retryAll.isPending}
              >
                {retryAll.isPending
                  ? "Retrying…"
                  : `Retry all failed (${run.node_failed})`}
              </button>
            )}
            {ACTIVE.has(run.status) && (
              <button
                className="btn small"
                onClick={() => cancel.mutate()}
                disabled={cancel.isPending}
              >
                Cancel run
              </button>
            )}
          </div>

          {!detail && <div className="activity-empty">Loading…</div>}
          {detail?.nodes.map((node) => (
            <NodeRow
              key={node.node_id}
              node={node}
              onRetry={() => retryNode.mutate(node.node_id)}
              retrying={retryNode.isPending}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function NodeRow({
  node,
  onRetry,
  retrying,
}: {
  node: WorkflowNodeRow;
  onRetry: () => void;
  retrying: boolean;
}) {
  const [showJobs, setShowJobs] = useState(false);

  // An input binds a file; it never runs. Rendering it with a state chip would
  // claim a step completed when nothing executed.
  if (node.kind === "input") {
    return (
      <div className="workflow-node-row input">
        <span className="workflow-node-name">{node.label}</span>
        <span className="workflow-node-kind">input</span>
      </div>
    );
  }

  return (
    <div className={`workflow-node-row ${node.state}`}>
      <button
        className="workflow-node-name"
        onClick={() => setShowJobs((s) => !s)}
        disabled={node.jobs.length === 0}
        title={
          node.jobs.length === 0
            ? "Not launched yet"
            : `${node.jobs.length} job(s)`
        }
      >
        {node.jobs.length > 0 && (
          <span className="workflow-run-chevron">{showJobs ? "▾" : "▸"}</span>
        )}
        {node.label}
      </button>

      <span className={`workflow-node-state ${node.state}`}>{node.state}</span>
      {node.attempt > 1 && (
        <span className="workflow-node-attempt">attempt {node.attempt}</span>
      )}
      {node.state === "failed" && (
        <button className="btn small" onClick={onRetry} disabled={retrying}>
          Retry
        </button>
      )}

      {showJobs && (
        <ul className="workflow-node-jobs">
          {node.jobs.map((job) => (
            <li key={job.job_id}>
              {/* A pruned job keeps its id but has no document left. Saying
                  "expired" is honest; inventing a state is not. */}
              <span className="workflow-job-type">{job.type ?? "expired"}</span>
              <span className="workflow-job-state">{job.state ?? "—"}</span>
              {/* `pct` is a fraction, not a percentage -- every other call
                  site multiplies by 100, and rendering it raw showed a
                  finished job as "1%". Shown only while running: a terminal
                  job's last progress reading is noise next to its state, and
                  "1%" beside "succeeded" reads as a contradiction. */}
              {job.state === "running" && job.progress?.pct != null && (
                <span className="workflow-job-pct">
                  {Math.round(job.progress.pct * 100)}%
                </span>
              )}
              {job.error && (
                <span className="workflow-job-error">{job.error.message}</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
