import { useCallback, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { formatRelative } from "../lib/format";
import type { MultiqcStatus } from "../api/types";
import type { JSX } from "react";

/** Unix seconds to the ISO string `formatRelative` takes.
 *
 * These timestamps come from `stat()` and job timings rather than from a
 * document field, so they arrive as seconds while every other timestamp
 * this app renders is an ISO string. Converting here keeps that seam in one
 * place instead of at each of the six call sites below. */
function ago(seconds: number | null): string {
  if (seconds === null) return "—";
  return formatRelative(new Date(seconds * 1000).toISOString());
}

/** Which of the seven states the panel is in.
 *
 * Pure and exported so the precedence is testable without a DOM -- the
 * ordering below is the whole design, and it is easy to get subtly wrong.
 * `building` outranks everything because a run in flight is the most
 * current fact; `failed` only wins when there is no report to offer, since
 * a report that exists is still worth opening even when a refresh failed.
 */
export type QcPanelState =
  | "building"
  | "stale"
  | "failed-with-report"
  | "ready"
  | "failed"
  | "unavailable"
  | "none";

export function panelState(s: MultiqcStatus): QcPanelState {
  if (s.running) return "building";
  if (s.generated_at !== null) {
    if (s.failed) return "failed-with-report";
    return s.stale ? "stale" : "ready";
  }
  if (s.failed) return "failed";
  return s.summarizable < 2 ? "unavailable" : "none";
}

/**
 * The project's aggregate QC report, in the left panel header.
 *
 * Project-scoped rather than hanging off the selected file, which is the
 * whole reason it lives here: a MultiQC report summarizes every file and
 * belongs to none of them, so filing it under whichever object launched it
 * would orphan it when that object is deleted.
 *
 * Seven states, and the combinations are the point -- a failed run with an
 * older report still on disk offers that report *and* says the refresh
 * failed, rather than collapsing to either message alone. The endpoint
 * returns independent flags so this component renders them rather than
 * inferring them.
 */
export function ProjectQcSummary({
  projectId,
}: {
  projectId: string;
}): JSX.Element | null {
  const [launching, setLaunching] = useState(false);

  // useQuery rather than useState-plus-fetch so this panel is reachable by the
  // centralised SSE invalidation in hooks/useEvents.ts. On its own local state
  // it was invisible to that, and a QC job finishing anywhere else in the app
  // left the stale/Regenerate state unshown until the component remounted --
  // the one view in a consistently SSE-driven app that behaved differently
  // (#886). The ["multiqc-status"] key is invalidated on job events there.
  const { data: status } = useQuery({
    queryKey: ["multiqc-status", projectId],
    queryFn: () => api.multiqcStatus(projectId),
    // A panel that cannot read its own state says nothing rather than showing
    // an error where a report link belongs. The report is a convenience; the
    // files below it are the page. `null` on failure preserves that.
    retry: false,
    // Poll only while a run is in flight. The job takes about two seconds, so
    // this is a short burst rather than a standing timer -- and it stops on
    // its own when `running` goes false, without needing the job id.
    refetchInterval: (q) =>
      (q.state.data as MultiqcStatus | undefined)?.running ? 1500 : false,
  });

  const qc = useQueryClient();

  const regenerate = useCallback(async () => {
    setLaunching(true);
    try {
      await api.launchMultiqc(projectId);
      await qc.invalidateQueries({ queryKey: ["multiqc-status", projectId] });
    } catch {
      // Swallowed deliberately: the next poll reports the real state, and
      // a conflict here means a run is already going -- which is what the
      // user wanted anyway.
    } finally {
      setLaunching(false);
    }
  }, [projectId, qc]);

  if (!status) return null;

  const reportLink = (
    <a
      href={api.multiqcReportUrl(projectId)}
      target="_blank"
      rel="noopener noreferrer"
      className="report-link project-qc-title"
    >
      MultiQC report
      <span className="report-link-icon" aria-hidden="true">
        ↗
      </span>
    </a>
  );

  const covered = status.covered ?? status.summarizable;
  const state = panelState(status);

  return (
    <div className="project-qc">
      <div
        className={
          state === "building"
            ? "project-qc-eyebrow is-running"
            : status.failed
              ? "project-qc-eyebrow is-failed"
              : "project-qc-eyebrow"
        }
      >
        {state === "building"
          ? "Project QC · building"
          : status.failed
            ? "Project QC · failed"
            : "Project QC"}
      </div>

      {state === "building" ? (
        <>
          <div className="project-qc-row">
            <span className="spinner" />
            <span className="project-qc-title">MultiQC report</span>
          </div>
          <div className="project-qc-note">
            Started {ago(status.running_since)} · usually under 2 seconds
          </div>
        </>
      ) : state === "ready" || state === "stale" || state === "failed-with-report" ? (
        <>
          {reportLink}
          <div className="project-qc-note">
            {covered} files · generated {ago(status.generated_at)}
          </div>
          {state === "failed-with-report" ? (
            <div className="project-qc-note is-error">
              The regeneration failed; this is still the older report.
            </div>
          ) : state === "stale" ? (
            <>
              <div className="project-qc-note is-warn">
                QC has run since this report was generated.
              </div>
              <button
                type="button"
                className="btn-text project-qc-action"
                onClick={() => void regenerate()}
                disabled={launching}
              >
                Regenerate
              </button>
            </>
          ) : null}
        </>
      ) : state === "failed" ? (
        <>
          <div className="project-qc-title is-muted">No report yet</div>
          <div className="project-qc-note">
            The last run failed {ago(status.failed_at)}. Activity has the
            captured output.
          </div>
          <button
            type="button"
            className="btn-text project-qc-action"
            onClick={() => void regenerate()}
            disabled={launching}
          >
            Try again
          </button>
        </>
      ) : state === "unavailable" ? (
        <>
          <div className="project-qc-title is-muted">Not available</div>
          <div className="project-qc-note">
            {status.summarizable === 1
              ? "Only one file carries parseable QC output. A summary needs at least two."
              : "No files carry parseable QC output yet."}
          </div>
        </>
      ) : (
        <>
          <div className="project-qc-title is-muted">No report yet</div>
          <div className="project-qc-note">
            {status.summarizable} files carry QC output. Summarize QC across
            files from any file&rsquo;s Actions tab.
          </div>
        </>
      )}
    </div>
  );
}
