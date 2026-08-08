import { useState } from "react";
import type { RunMemberJob, RunSummary } from "../../api/types";
import { formatClock } from "../../lib/format";
import { STATUS_LABELS, runFacts } from "../../lib/runFormat";
import { SectionHead } from "./SectionHead";
import { RunFailureBlock } from "./RunFailureBlock";

/**
 * Finished runs as a numbered ledger: one line each, parameters on demand.
 *
 * The line is deliberately not a `RunRow` -- that component leads with a
 * chevron and a status chip because it lived in a full-width list. Here the
 * column is narrow and the runs are all in the past, so the label gets the
 * space and everything else is set small and right-aligned.
 */
export function RunLedger({
  runs,
  jobsByRun,
  onSelect,
}: {
  runs: RunSummary[];
  /** Member jobs per run id, from the details already fetched by the page.
   *  The count comes from this too -- the array is what a failed row needs to
   *  say anything about why it failed. */
  jobsByRun: Map<string, RunMemberJob[]>;
  onSelect: (objectId: string, projectId: string) => void;
}) {
  // One open at a time: the column is short, and two expanded rows push the
  // rest off the fold for no gain.
  const [open, setOpen] = useState<string | null>(null);

  return (
    <section className="activity-ledger">
      <SectionHead title="Recent runs" note={`last ${runs.length}`} />

      {runs.length === 0 ? (
        <div className="activity-empty">No finished runs.</div>
      ) : (
        <>
          {runs.map((run, i) => (
            <LedgerRow
              key={run.id}
              run={run}
              index={i + 1}
              jobs={jobsByRun.get(run.id)}
              open={open === run.id}
              onToggle={() => setOpen((o) => (o === run.id ? null : run.id))}
              onSelect={onSelect}
            />
          ))}
          <div className="activity-hint">Click a line to open its parameters.</div>
        </>
      )}
    </section>
  );
}

/**
 * One run as a ledger line.
 *
 * Also used for the second and subsequent in-progress runs, which is why the
 * index is a prop rather than derived here -- the lead column numbers its
 * overflow continuing from the featured run.
 */
export function LedgerRow({
  run,
  index,
  jobs,
  open,
  onToggle,
  onSelect,
}: {
  run: RunSummary;
  index: number;
  jobs?: RunMemberJob[];
  open: boolean;
  onToggle: () => void;
  onSelect: (objectId: string, projectId: string) => void;
}) {
  const facts = runFacts(run);
  const jobCount = jobs?.length;
  const meta = [
    STATUS_LABELS[run.status],
    jobCount != null ? `${jobCount} ${jobCount === 1 ? "job" : "jobs"}` : null,
    formatClock(run.updated_at),
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <div className={`ledger-row ${run.status}`}>
      <div
        className="ledger-line"
        role="button"
        tabIndex={0}
        aria-expanded={open}
        onClick={onToggle}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onToggle();
          }
        }}
      >
        <span className="ledger-num">{String(index).padStart(2, "0")}</span>
        <span className="ledger-title">{run.label}</span>
        <span className="ledger-meta">{meta}</span>
      </div>

      {open && (
        <div className="ledger-detail">
          <div className="ledger-facts">
            {facts.map((f) => (
              <div key={f.k} className="activity-fact">
                <span className="activity-fact-k">{f.k}</span>
                <span className="activity-fact-v">{f.v}</span>
              </div>
            ))}
          </div>

          {/* The inputs are the way back to the files themselves; the fact grid
              above shows their names but cannot carry a control. */}
          {run.inputs.length > 0 && (
            <div className="ledger-links">
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

          {/* Only for a run that failed. A succeeded run's expansion is
              exactly what it was before this existed. */}
          {run.status === "failed" && <RunFailureBlock jobs={jobs ?? []} />}
        </div>
      )}
    </div>
  );
}
