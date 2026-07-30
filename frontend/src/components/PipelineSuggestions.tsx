import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { notify } from "../stores/messageStore";
import type { PipelineSuggestion } from "../api/types";

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
export function PipelineSuggestions({ objectId }: { objectId: string }) {
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
            <div className="suggestion-category">{card.category}</div>
            <div className="suggestion-title">{card.title}</div>
            <div className="suggestion-desc">{card.description}</div>
            {(card.why ?? card.reason) && (
              <div className="suggestion-why">{card.why ?? card.reason}</div>
            )}
            <button
              type="button"
              className={card === firstAvailable ? "btn primary" : "btn"}
              onClick={() => launch.mutate(card)}
              disabled={!available || launch.isPending}
            >
              Launch
            </button>
          </div>
        );
      })}
    </div>
  );
}
