import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "../api/client";
import type { AiSummaryFacts } from "../api/types";
import { notify } from "../stores/messageStore";

/**
 * A few sentences about what this file's numbers mean, written by a local model.
 *
 * The whole section is additive and self-suppressing in both directions: with
 * no model server running and no stored summary it renders nothing at all, so a
 * user who has never started one sees the app exactly as it was. That is the
 * governing constraint -- this is a nicety layered on facts the app already
 * has, and it must never present itself as something the user has to attend to.
 *
 * Two things are deliberately always visible when a summary is shown: the model
 * that wrote it, and whether it still describes the file. A narrative that
 * sounds authoritative but was written before the last QC run is the specific
 * failure this section has to make impossible to walk into, so a stale summary
 * says so in the header rather than being silently replaced or silently kept.
 */
export function AiSummary({
  facts,
  objectId,
  fingerprint,
}: {
  facts: Record<string, unknown>;
  objectId: string;
  /**
   * The digest of the object's *current* facts and metadata, computed by the
   * server. Compared against the one stored with the summary to detect
   * staleness. Undefined when the server did not supply one, in which case
   * staleness is simply not claimed either way.
   */
  fingerprint?: string;
}) {
  const summary = facts as AiSummaryFacts;
  const queryClient = useQueryClient();

  // Whether a model is reachable right now. Not retried and not refetched on
  // focus: a down server is an ordinary state here, and hammering a port that
  // is not listening to re-confirm that would be noise for no benefit.
  const { data: status } = useQuery({
    queryKey: ["summary", "status"],
    queryFn: () => api.summaryStatus(),
    retry: false,
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  });

  const regenerate = useMutation({
    mutationFn: () => api.launchSummary(objectId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
      notify.info("Summary queued");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const existing = summary.ai_summary?.trim();
  const available = status?.available === true;

  // Nothing stored and nowhere to generate from: render nothing rather than an
  // empty section advertising a feature that is not currently possible.
  if (!existing && !available) return null;

  const stale =
    existing != null &&
    fingerprint != null &&
    summary.ai_summary_fingerprint != null &&
    summary.ai_summary_fingerprint !== fingerprint;

  return (
    <div className="section">
      <div className="section-title">
        Summary
        {/* Reuses the existing warn treatment rather than inventing a variant,
            so "needs attention" looks the same here as everywhere else. */}
        {stale && <span className="badge quarantined">Out of date</span>}
      </div>

      {existing ? (
        <>
          <p className="ai-summary-body">{existing}</p>
          <div className="section-note">
            {stale
              ? "Written before the most recent changes to this file's data — regenerate to bring it up to date."
              : "Generated from this file's measurements and metadata."}
            {summary.ai_summary_model && ` Model: ${summary.ai_summary_model}.`}
            {summary.ai_summary_at && ` ${formatWhen(summary.ai_summary_at)}.`}
          </div>
        </>
      ) : (
        <div className="section-note">
          No summary yet for this file.
        </div>
      )}

      {available && (
        <button
          type="button"
          className="btn-text"
          onClick={() => regenerate.mutate()}
          disabled={regenerate.isPending}
        >
          {regenerate.isPending
            ? "Queueing…"
            : existing
              ? "Regenerate"
              : "Write a summary"}
        </button>
      )}
    </div>
  );
}

/** "on 30 Jul 2026", or nothing at all if the timestamp will not parse. */
function formatWhen(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "";
  return `Written ${at.toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
  })}`;
}
