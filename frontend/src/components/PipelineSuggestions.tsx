import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { notify } from "../stores/messageStore";
import type { PipelineSuggestion, PriorRun } from "../api/types";

/** What this card has already produced.
 *
 * Failed runs are listed deliberately. They have no output to link, so the
 * status word carries the row -- and a user who cannot see that the last two
 * launches failed is a user about to launch a third time.
 */
function PriorRuns({
  runs,
  projectId,
}: {
  runs: PriorRun[];
  projectId: string;
}) {
  return (
    <div className="prior-runs">
      {runs.map((run) => (
        <div key={run.run_id} className="prior-run">
          <span className="prior-run-date">
            {new Date(run.finished_at).toLocaleDateString(undefined, {
              month: "short",
              day: "numeric",
            })}
          </span>
          <span className="prior-run-outputs">
            {run.outputs.map((out) =>
              out.exists ? (
                <Link
                  key={out.object_id}
                  to={`/p/${projectId}?sel=object:${out.object_id}`}
                  className="prior-run-link"
                >
                  {out.name}
                </Link>
              ) : (
                <span key={out.object_id} className="prior-run-gone">
                  {out.name}
                </span>
              ),
            )}
          </span>
          <span className={`prior-run-status is-${run.status}`}>
            {run.status}
          </span>
        </div>
      ))}
    </div>
  );
}

/**
 * What this file can be run through next, and why.
 *
 * Each card is either runnable -- with the reason it is a sensible default --
 * or gated, with the honest reason it cannot run. There is no third state
 * where a gated card quietly runs its own prerequisite first.
 *
 * The suggestions are advisory. If the request fails, the Actions tab loses
 * its cards but keeps every manual control, so a failure here is reported as
 * a one-line note rather than an error box -- a broken advisory should not
 * make a healthy file look broken.
 */
export function PipelineSuggestions({
  objectId,
  projectId,
}: {
  objectId: string;
  projectId: string;
}) {
  const qc = useQueryClient();

  // No `enabled` guard: this component only mounts inside the Actions tab, so
  // mounting *is* the "only when the tab is open" condition. A flag here would
  // restate that in a second place that could disagree with it.
  const { data, isLoading, isError } = useQuery({
    queryKey: ["suggestions", objectId],
    queryFn: () => api.suggestions(objectId),
  });

  const launch = useMutation({
    // The card carries the complete body for its own endpoint. Posting it
    // verbatim is what keeps this component ignorant of the three launch
    // request shapes -- see `PipelineSuggestion`.
    mutationFn: (card: PipelineSuggestion) =>
      api.launchSuggestion(card.launch!.endpoint, card.launch!.body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      // The launched job changes what should be offered next, so the cards
      // themselves are stale the moment one runs.
      qc.invalidateQueries({ queryKey: ["suggestions", objectId] });
      notify.success("Queued");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  if (isLoading) return null;

  const cards = data?.suggestions ?? [];
  if (isError || cards.length === 0) {
    return (
      <p className="suggestion-none">
        {isError
          ? "Could not load pipeline suggestions."
          : "No pipeline suggestions for this file."}
      </p>
    );
  }

  // One primary action per grid, so the eye lands somewhere. The rest are
  // still runnable, just not the assumed next step.
  const firstAvailable = cards.find((c) => c.status === "available");

  return (
    <div className="suggestion-grid">
      {cards.map((card) => {
        const available = card.status === "available";
        return (
          <div key={card.kind} className="suggestion-card">
            <div className="suggestion-card-top">
              <div className="suggestion-category">{card.category}</div>
              {card.prior_runs.length > 0 && (
                <div className="prior-runs-count">
                  {card.prior_runs.length} prior run
                  {card.prior_runs.length === 1 ? "" : "s"}
                </div>
              )}
            </div>
            <div className="suggestion-title">{card.title}</div>
            <div className="suggestion-desc">{card.description}</div>
            {(card.why ?? card.reason) && (
              <div className="suggestion-why">{card.why ?? card.reason}</div>
            )}
            {card.prior_runs.length > 0 && (
              <PriorRuns runs={card.prior_runs} projectId={projectId} />
            )}
            <button
              type="button"
              className={card === firstAvailable ? "btn primary" : "btn"}
              onClick={() => launch.mutate(card)}
              disabled={!available || launch.isPending}
            >
              {card.prior_runs.length > 0 ? "Launch again" : "Launch"}
            </button>
          </div>
        );
      })}
    </div>
  );
}
