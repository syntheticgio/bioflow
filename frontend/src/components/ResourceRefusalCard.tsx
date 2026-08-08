import type { ReplanResult } from "../api/types";

/**
 * The four exits from a resource refusal.
 *
 * One component, two triggers. AlignDialog renders it pre-flight from its own
 * client-side band computation; AssembleDialog renders it reactively from a
 * 422 body. Both produce this same props shape.
 */
export interface ResourceRefusalCardProps {
  estimateMb: number;
  budgetMb: number;
  /** The prose phrase from memory_estimate.resolve() -- "from 23 previous
   *  runs on this machine" or "from published tool coefficients". */
  detail: string;
  /** The full explanation sentence naming the dominant term. */
  explanation: string;
  /** null while the replan request is still in flight. */
  replan: ReplanResult | null;
  onCancel: () => void;
  onEdit: () => void;
  onLaunchAnyway: () => void;
  onAcceptReplan: (params: Record<string, unknown>) => void;
}

export function ResourceRefusalCard({
  estimateMb,
  budgetMb,
  detail,
  explanation,
  replan,
  onCancel,
  onEdit,
  onLaunchAnyway,
  onAcceptReplan,
}: ResourceRefusalCardProps) {
  const proposal = replan?.kind === "proposal" ? replan : null;

  return (
    <div className="error-box" style={{ marginBottom: 12 }}>
      <div style={{ fontWeight: 600, marginBottom: 4 }}>
        This will not fit in the memory budget
      </div>

      <div>{explanation}</div>

      {/* An acceptance criterion, and decision-relevant rather than
          diagnostic: a published coefficient deserves less deference than a
          measurement, and this is what the user is overriding below.
          r_squared is deliberately absent -- resolve() already falls back to
          the heuristic when a measured estimate extrapolates too far, so any
          measured number reaching here is inside its own guard rails. */}
      <div style={{ marginTop: 4, opacity: 0.85 }}>
        Estimated {estimateMb.toLocaleString()} MB {detail}, against a{" "}
        {budgetMb.toLocaleString()} MB budget.
      </div>

      {proposal && (
        <div style={{ marginTop: 8 }}>
          <div style={{ fontWeight: 600 }}>A smaller configuration fits:</div>
          <ul style={{ margin: "4px 0 0", paddingLeft: 20 }}>
            {proposal.changes.map((c) => (
              <li key={c.name}>
                {c.name}: {c.before.toLocaleString()} →{" "}
                {c.after.toLocaleString()}
              </li>
            ))}
          </ul>
          {/* Reported separately from the knob diff on purpose: a capacity
              clamp is a fact about the hardware, while the diff is a fact
              about the budget. Collapsing them loses the explanation a user
              who over-requested threads most needs. */}
          {proposal.note && (
            <div style={{ marginTop: 4 }}>{proposal.note}</div>
          )}
          <div style={{ marginTop: 4, opacity: 0.85 }}>
            Estimated {proposal.estimate_mb.toLocaleString()} MB. Fewer threads
            means a longer run.
          </div>
        </div>
      )}

      {replan?.kind === "infeasible" && (
        <div style={{ marginTop: 8 }}>{replan.reason}</div>
      )}

      {replan?.kind === "no_knobs" && (
        <div style={{ marginTop: 8 }}>
          There is nothing to adjust automatically for this job.
        </div>
      )}

      <div
        style={{
          marginTop: 12,
          display: "flex",
          gap: 8,
          flexWrap: "wrap",
          alignItems: "center",
        }}
      >
        <button type="button" className="btn" onClick={onCancel}>
          Cancel
        </button>
        <button type="button" className="btn" onClick={onEdit}>
          Edit parameters
        </button>
        {/* Renders only for a verified proposal -- the design's guarantee that
            the button is never offered and then refused. */}
        {proposal && (
          <button
            type="button"
            className="btn primary"
            onClick={() => onAcceptReplan(proposal.params)}
          >
            Use the smaller configuration
          </button>
        )}
        <button type="button" className="btn" onClick={onLaunchAnyway}>
          Launch anyway
        </button>
      </div>

      {/* The consequence is stated where the button is, not behind a second
          click: a confirmation step would put the most friction on the
          least-used exit, and the card is already the confirmation. Worded so
          it promises no safety net -- the budget is the user's configured
          limit, so a limit set above physical RAM can still exhaust it. */}
      <div style={{ marginTop: 6, opacity: 0.85, fontSize: "0.9em" }}>
        Launching anyway runs this job only when nothing else is running, and
        it may use more than your configured limit.
      </div>
    </div>
  );
}
