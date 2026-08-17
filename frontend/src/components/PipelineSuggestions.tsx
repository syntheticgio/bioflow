import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { formatBytes } from "../lib/format";
import { notify } from "../stores/messageStore";
import type { PipelineSuggestion, PriorRun } from "../api/types";
import { NodeSelector } from "./NodeSelector";

/** A job this grid started, and when. See `launched` for why the id is kept. */
interface LaunchedJob {
  id: string;
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
  const { data, isLoading, isError } = useQuery({
    queryKey: ["suggestions", objectId],
    queryFn: () => api.suggestions(objectId),
  });

  // The same query the detail panel and ActivePipelineJobs already make for
  // this file, so all three share one poll rather than each running their own.
  const { data: activeJobs, dataUpdatedAt } = useQuery({
    queryKey: ["jobs", "for-object", objectId],
    queryFn: () => api.listJobs({ objectId, states: "active", limit: 20 }),
    // A pipeline run publishes no event this component listens to, so poll
    // often enough that a finished run re-enables its card promptly.
    refetchInterval: 5_000,
  });

  // Which card launched which job, so a running job greys out the one button
  // that started it rather than the whole grid. Keyed by `kind` because that
  // is what the grid keys its cards by.
  //
  // Recorded here rather than derived from the job's `type` because no honest
  // mapping from card to job type exists: a card's kind ("assemble") is not a
  // job type, one launch can fan out into several jobs (align builds an index
  // first), and a hand-maintained kind-to-type table is exactly the registry
  // that silently skips the next card someone adds. The launch response
  // carries the job's real id, which needs no table to stay correct.
  //
  // Component state, so a page reload during a long run re-enables the button.
  // Accepted rather than worked around: reconstructing it server-side would
  // mean matching a running job back to the card that offered it, and the one
  // existing attempt at that mapping (`prior_runs._CARD_RUN_KINDS`) covers two
  // of the twenty-odd card kinds and silently matches nothing for the rest.
  // A guard that is correct while the tab stays open beats one that is wrong
  // for most cards in a way nothing reports. `ActivePipelineJobs`, rendered on
  // this same panel, still shows the run after a reload.
  const [launched, setLaunched] = useState<Record<string, LaunchedJob>>({});

  const launch = useMutation({
    // The card carries the complete body for its own endpoint. Posting it
    // verbatim is what keeps this component ignorant of the three launch
    // request shapes -- see `PipelineSuggestion`.
    mutationFn: (card: PipelineSuggestion) =>
      api.launchSuggestion(card.launch!.endpoint, card.launch!.body, targetNode || undefined),
    onSuccess: (job, card) => {
      setLaunched((m) => ({
        ...m,
        [card.kind]: { id: job.id, at: Date.now() },
      }));
      qc.invalidateQueries({ queryKey: ["jobs"] });
      // The launched job changes what should be offered next, so the cards
      // themselves are stale the moment one runs.
      qc.invalidateQueries({ queryKey: ["suggestions", objectId] });
      notify.success("Queued");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const activeIds = new Set((activeJobs ?? []).map((j) => j.id));

  /**
   * Is this card's launch still in flight?
   *
   * A job is busy while it is queued, blocked, or running -- `states=active`
   * is the queue's own definition of that, so a state added later is covered
   * without this component knowing about it. It stops being busy the moment
   * it reaches a terminal state, whether that is success or failure: a failed
   * run must re-enable the button, since retrying is exactly what the user
   * wants next.
   *
   * The `dataUpdatedAt` guard closes the window between the POST returning
   * and the first poll that can see the new job. Without it the job is absent
   * from the cached list for the same reason a finished job is -- so the
   * button would flick back to enabled for a few seconds immediately after
   * being pressed, which is the exact behaviour this is fixing.
   */
  function busyFor(kind: string): boolean {
    const rec = launched[kind];
    if (!rec) return false;
    if (activeIds.has(rec.id)) return true;
    return dataUpdatedAt <= rec.at;
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
        const busy = busyFor(card.kind);
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
