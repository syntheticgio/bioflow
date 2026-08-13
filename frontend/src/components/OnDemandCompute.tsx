import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type ReactNode } from "react";
import { api } from "../api/client";
import { notify } from "../stores/messageStore";

export type JobType =
  | "run_qc"
  | "run_bam_stats"
  | "run_transcript_qc"
  | "run_vcf_stats"
  | "run_annotation_stats";

export interface ComputeCtx {
  /** The built-in recompute button, wired to the component's mutation and
   *  guard.  Rendered inline: `<button style={{…small accent…}} …/>`. */
  recomputeButton: ReactNode;
}

export interface OnDemandComputeProps {
  /** Object id (for the `activeJobs` guard and launch). */
  objectId: string;
  /** Job type this surface launches; used to detect a matching in-flight job. */
  jobType: JobType;
  /** Launches the job. A thunk — the caller closes over targetNode / GTF id. */
  launch: () => Promise<unknown>;
  /** Toast message on successful enqueue. Default `"Computing results"`. */
  successMessage?: string;
  /** Whether the status-fact is present; gates empty state vs. results. */
  hasResults: boolean;
  /** Empty-state heading. */
  title: string;

  // -- empty-state layout ------------------------------------------------

  /** Rendered before the title: `NodeSelector`, etc. Optional. */
  preflight?: ReactNode;
  /** The note or warning block below the title. For a plain `section-note`
   *  this is all that is needed — the component renders the compute-button
   *  after it. */
  body?: ReactNode;
  /** Full control over everything below the title: note + button.
   *  When provided, `body` is ignored.  TranscriptQc uses this to place its
   *  GTF `<select>` and the compute button inline under its own conditional
   *  logic.  Receives the component-built `computeButton` node so the
   *  surface does not need to rebuild it. */
  renderBody?: (computeButton: ReactNode) => ReactNode;
  /** Compute button label. Default `"Compute results"`. */
  computeLabel?: string;
  /** Final button class: `"btn"` or `"btn primary"`. Default `"btn primary"`. */
  buttonClass?: string;
  /** Extra disable reason folded into the button's `disabled` alongside
   *  the activeJobs guard (isRunning) and the mutation's isPending. */
  disabled?: boolean;

  // -- results-mode ------------------------------------------------------

  /** The results view, rendered when `hasResults` is true.
   *
   *  Called with the built-in recompute-button node so each surface can place
   *  it exactly where it sits today — top of `qc-provenance` (Variant,
   *  Annotation), bottom of the Provenance `.section` (BAM), or not at all
   *  (TranscriptQc). */
  children: (ctx: ComputeCtx) => ReactNode;
}

/**
 * Shared shell for on-demand compute surfaces: empty state with a launch
 * button, an in-flight guard against double-submit, and a recompute button
 * passed to the results view via render-props.
 *
 * Four Results-tab surfaces use this: `BamResults`, `VariantResults`,
 * `TranscriptQc`, `AnnotationResults`.  `DetailPanel`'s FASTQ QC is a
 * different seam (guards two job types, already has its own in-flight guard)
 * and does not use this component.
 */
export function OnDemandCompute({
  objectId,
  jobType,
  launch,
  successMessage = "Computing results",
  hasResults,
  title,
  preflight,
  body,
  renderBody,
  computeLabel = "Compute results",
  buttonClass = "btn primary",
  disabled = false,
  children,
}: OnDemandComputeProps) {
  const qc = useQueryClient();

  const { data: activeJobs } = useQuery({
    queryKey: ["jobs", "for-object", objectId],
    queryFn: () => api.listJobs({ objectId, states: "active", limit: 20 }),
    refetchInterval: 5_000,
  });

  const isRunning = (activeJobs ?? []).some((j) => j.type === jobType);

  const compute = useMutation({
    mutationFn: launch,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      notify.info(successMessage);
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const isBusy = compute.isPending || isRunning;

  const computeButton = (
    <button
      type="button"
      className={buttonClass}
      onClick={() => compute.mutate()}
      disabled={isBusy || disabled}
    >
      {isBusy ? "Computing…" : computeLabel}
    </button>
  );

  const recomputeButton = (
    <button
      type="button"
      onClick={() => compute.mutate()}
      disabled={isBusy}
      style={{
        color: "var(--accent)",
        fontSize: 11,
        textTransform: "none",
        letterSpacing: 0,
      }}
    >
      {isBusy ? "recomputing…" : "recompute results"}
    </button>
  );

  if (!hasResults) {
    return (
      <div className="section">
        {preflight}
        <div className="section-title">{title}</div>
        {renderBody
          ? renderBody(computeButton)
          : <>
              {body}
              {computeButton}
            </>
        }
      </div>
    );
  }

  return <>{children({ recomputeButton })}</>;
}