import { useState } from "react";
import { api } from "../../api/client";

/**
 * "Explain this error" -- click-triggered only, never generated
 * automatically on job failure. A model that is not configured or that
 * produces nothing means the button simply does not appear; the raw
 * error text above it is never replaced or hidden.
 *
 * Lives here rather than in ActivityView so that both the loose-job list
 * and the run ledger can use it. The ledger is a child of ActivityView, so
 * importing it from there would be a backwards dependency.
 */
export function FailureExplanationExpander({
  code,
  message,
}: {
  code: string;
  message: string;
}) {
  const [state, setState] = useState<
    | { status: "idle" }
    | { status: "loading" }
    | { status: "unavailable" }
    | { status: "shown"; text: string; model: string | null }
  >({ status: "idle" });

  if (state.status === "unavailable") return null;

  if (state.status === "shown") {
    return (
      <div style={{ marginTop: 4, color: "var(--text-faint)" }}>
        {state.text}
        {state.model && (
          <span style={{ color: "var(--text-faint)" }}> (AI-generated, {state.model})</span>
        )}
      </div>
    );
  }

  return (
    <button
      type="button"
      className="btn-text"
      style={{ marginLeft: 8 }}
      disabled={state.status === "loading"}
      onClick={async () => {
        setState({ status: "loading" });
        try {
          const result = await api.failureExplanation(code, message);
          if (result == null) {
            setState({ status: "unavailable" });
          } else {
            setState({ status: "shown", text: result.text, model: result.model });
          }
        } catch {
          setState({ status: "unavailable" });
        }
      }}
    >
      {state.status === "loading" ? "Explaining…" : "Explain this error"}
    </button>
  );
}
