import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { formatBytes } from "../lib/format";
import { notify } from "../stores/messageStore";
import type { PipelineSuggestion, PriorRun } from "../api/types";
import { NodeSelector } from "./NodeSelector";

/** When this grid last launched a given card. See `launched`. */
interface LaunchedJob {
  at: number;
}

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
  const [targetNode, setTargetNode] = useState("");

  // No `enabled` guard: this component only mounts inside the Actions tab, so
  // mounting *is* the "only when the tab is open" condition. A flag here would
  // restate that in a second place that could disagree with it.
  // `card.running` is part of this payload and changes without anything the
  // user does -- a queued job starts, a running one finishes -- so the cards
  // are polled rather than fetched once. Same interval as the active-jobs
  // query the rest of the panel uses, since they answer the same question
  // about the same file.
  const { data, isLoading, isError, dataUpdatedAt } = useQuery({
    queryKey: ["suggestions", objectId],
    queryFn: () => api.suggestions(objectId),
    refetchInterval: 5_000,
    // Overrides the app-wide `refetchOnWindowFocus: false`, and is not
    // optional here. React Query pauses `refetchInterval` while the tab is
    // hidden, so without this a user who switches away mid-run and comes back
    // sees whatever `running` was true when they left -- and because this
    // field disables a button, a stale value is not a stale label but a
    // control they cannot press until the next tick lands.
    refetchOnWindowFocus: true,
  });

  // Cards this tab launched, and when. The server's `card.running` is the
  // authority -- it survives a reload and sees runs the Computations dialog
  // started -- but it only updates when the poll above comes back. This
  // covers the seconds in between, so the button greys on the click rather
  // than on the next refetch.
  //
  // Keyed by `kind`, which is what the grid keys its cards by.
  const [launched, setLaunched] = useState<Record<string, LaunchedJob>>({});

  const launch = useMutation({
    // The card carries the complete body for its own endpoint. Posting it
    // verbatim is what keeps this component ignorant of the three launch
    // request shapes -- see `PipelineSuggestion`.
    mutationFn: (card: PipelineSuggestion) =>
      api.launchSuggestion(card.launch!.endpoint, card.launch!.body, targetNode || undefined),
    onSuccess: (_job, card) => {
      setLaunched((m) => ({ ...m, [card.kind]: { at: Date.now() } }));
      qc.invalidateQueries({ queryKey: ["jobs"] });
      // The launched job changes what should be offered next, so the cards
      // themselves are stale the moment one runs.
      qc.invalidateQueries({ queryKey: ["suggestions", objectId] });
      notify.success("Queued");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  /**
   * Is this card's work in flight?
   *
   * `card.running` is the real answer: the server checks the queue for a
   * non-terminal job of the type this card's endpoint produces, so it is
   * right after a reload and right about runs this tab never launched.
   * Queued and blocked count as running; a job that succeeds *or* fails is
   * terminal and re-enables the button, since retrying a failure is what the
   * user wants next.
   *
   * The local record only covers the gap before the next poll returns, and
   * only forwards -- it can add "running", never remove it. Once a refetch
   * that post-dates the launch has come back, the server's answer stands
   * alone, so a finished job re-enables the button even though this tab still
   * remembers launching it.
   */
  function busyFor(card: PipelineSuggestion): boolean {
    if (card.running) return true;
    const rec = launched[card.kind];
    return rec != null && dataUpdatedAt <= rec.at;
  }

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
    <>
    {cards.length > 0 && (
      <NodeSelector value={targetNode} onChange={setTargetNode} />
    )}
    <div className="suggestion-grid">
      {cards.map((card) => {
        // "needs_install" is not blocked -- it is one click from working,
        // the same as "available", so it gets the same enabled button. The
        // distinction from "available" is purely in what the button says and
        // whether a size is shown, not in whether it can be pressed:
        // rendering this as disabled (the "unavailable" treatment) would be
        // the worst of the two wrong answers, since the card would then read
        // as a permanent dead end and the user would never learn DeepVariant
        // -- or whatever tool -- exists at all.
        const runnable = card.status === "available" || card.status === "needs_install";
        const busy = busyFor(card);
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
            {card.status === "needs_install" && card.requires_install?.download_bytes && (
              <div className="suggestion-install-size">
                Downloads {formatBytes(card.requires_install.download_bytes)} on first use
              </div>
            )}
            {card.prior_runs.length > 0 && (
              <PriorRuns runs={card.prior_runs} projectId={projectId} />
            )}
            <button
              type="button"
              className={card === firstAvailable ? "btn primary" : "btn"}
              onClick={() => launch.mutate(card)}
              // `launch.isPending` covers only the POST, and only for whichever
              // card is mid-request; `busy` is what keeps this one card greyed
              // for the life of the job it started.
              disabled={!runnable || launch.isPending || busy}
            >
              {busy
                ? "Running…"
                : card.status === "needs_install"
                  ? "Install and launch"
                  : card.prior_runs.length > 0
                    ? "Launch again"
                    : "Launch"}
            </button>
          </div>
        );
      })} 
    </div>
    </>
  );
}
