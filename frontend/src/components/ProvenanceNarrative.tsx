import { useMutation, useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { useState } from "react";

import { api } from "../api/client";
import type { ProvenanceStep } from "../api/types";
import { notify } from "../stores/messageStore";

/**
 * One numbered lineage row.
 *
 * Three lines, in decreasing weight: what the row is about, how it was made,
 * and the parameters it ran with. Gaps render inline in the position their
 * fact would have occupied -- a reader scanning for the version sees the
 * question asked and unanswered rather than seeing nothing and assuming it
 * did not matter. The same gaps are also listed in the rail, which is where
 * they are actionable; here they are context.
 */
function LineageRow({ step, index }: { step: ProvenanceStep; index: number }) {
  const params = Object.entries(step.params);

  // The verb line: "Trimmed with trimmomatic 0.39", or the gaps that stand in
  // for the parts that were never recorded.
  const description: ReactNode[] = [];
  if (step.verb) {
    const tool = step.tool ?? "an unrecorded tool";
    const version = step.tool_version ? ` ${step.tool_version}` : "";
    description.push(`${step.verb} ${tool}${version}`);
  } else {
    // A root: nothing produced it inside this app, which is not a gap.
    description.push("Input to this project");
  }

  return (
    <>
      <div className="lineage-index">{String(index + 1).padStart(2, "0")}</div>
      <div className="lineage-body">
        <div className="lineage-name">{step.name}</div>
        <div className="lineage-desc">
          {description}
          {step.gaps.map((g) => (
            <span key={g}>
              {" — "}
              <span className="lineage-gap">{g}</span>
            </span>
          ))}
          {step.used_by && (
            <span className="lineage-used-by">
              {" · used by "}
              {step.used_by}
            </span>
          )}
        </div>
        {params.length > 0 ? (
          <div className="lineage-params">
            {params
              .map(([k, v]) => `${k}=${String(v)}`)
              .join(" · ")}
          </div>
        ) : (
          step.job_type && <div className="lineage-params mono">{step.job_type}</div>
        )}
      </div>
    </>
  );
}

/**
 * The History tab: how this file was made, what has run on it, and the same
 * facts as a citable paragraph.
 *
 * The structured lineage is the deliverable and renders unconditionally --
 * it works with no AI provider configured, and it is what a user cites. The
 * paragraph is a second rendering of exactly these facts, fetched only on
 * click because it costs a model call and most opens of this panel do not
 * want one.
 *
 * `runs` is passed in rather than rendered here: the runs table owns its own
 * query, but the mockup places it in this column, so this component owns the
 * two-column layout and takes it as a child.
 */
export function ProvenanceNarrative({
  objectId,
  runs,
}: {
  objectId: string;
  runs?: ReactNode;
}) {
  const [copied, setCopied] = useState<"report" | "prose" | null>(null);

  const { data, isLoading, isError } = useQuery({
    queryKey: ["provenance-narrative", objectId],
    queryFn: () => api.getProvenanceNarrative(objectId),
  });

  const prose = useMutation({
    mutationFn: () => api.generateProvenanceProse(objectId),
    onError: (e: Error) => notify.error(e.message),
  });

  const copy = (text: string, which: "report" | "prose") => {
    void navigator.clipboard.writeText(text);
    setCopied(which);
    setTimeout(() => setCopied(null), 2000);
  };

  if (isLoading) {
    return <div className="section-note">Loading provenance…</div>;
  }

  if (isError || !data) {
    return <div className="error-box">Could not load provenance.</div>;
  }

  const proseText = prose.data?.prose ?? null;
  const gapCount = data.gaps.length;

  return (
    <div className="history-layout">
      <div>
        <div className="history-head">
          <div className="section-title">How this file was made</div>
          <button
            type="button"
            className="btn-text"
            onClick={() => copy(data.markdown, "report")}
          >
            {copied === "report" ? "Copied" : "Copy report"}
          </button>
        </div>
        <div className="section-note">Lineage, oldest first.</div>

        {data.lineage.length === 0 ? (
          <div className="section-note">No lineage has been recorded.</div>
        ) : (
          <div className="lineage-grid">
            {data.lineage.map((step, i) => (
              <LineageRow key={step.object_id} step={step} index={i} />
            ))}
          </div>
        )}

        {data.has_branches && (
          <div className="section-note" style={{ marginTop: 8 }}>
            This lineage has a branch: a step above combined more than one input.
          </div>
        )}

        {runs}
      </div>

      <div>
        <div className="section-title">Methods paragraph</div>

        {proseText ? (
          <>
            <div className="section-note">
              Generated from {data.lineage.length} lineage{" "}
              {data.lineage.length === 1 ? "step" : "steps"}.
            </div>
            <p className="ai-summary-body">{proseText}</p>
            <div className="detail-actions">
              <button
                type="button"
                className="btn-text"
                onClick={() => copy(proseText, "prose")}
              >
                {copied === "prose" ? "Copied" : "Copy paragraph"}
              </button>
              <button
                type="button"
                className="btn-text"
                onClick={() => prose.mutate()}
                disabled={prose.isPending}
              >
                Regenerate
              </button>
            </div>
          </>
        ) : (
          <>
            <div className="section-note">
              The lineage on the left, written as one citable paragraph.
            </div>
            <div className="prose-invite">
              <div className="prose-invite-body">
                {gapCount > 0
                  ? `${gapCount} ${gapCount === 1 ? "fact is" : "facts are"} missing, so the paragraph will say “an unrecorded tool”. Filling them in gives a cleaner sentence.`
                  : "Every fact this needs is recorded."}
              </div>
              <button
                type="button"
                className="btn"
                onClick={() => prose.mutate()}
                disabled={prose.isPending}
              >
                {prose.isPending ? "Generating…" : "Generate paragraph"}
              </button>
            </div>
          </>
        )}

        {prose.data?.unavailable_reason && (
          <div className="section-note">{prose.data.unavailable_reason}</div>
        )}

        {gapCount > 0 && (
          <>
            <div className="group-title" style={{ marginTop: 30 }}>
              Not recorded ({gapCount})
            </div>
            <div className="gap-rail">
              {data.gaps.map((gap, i) => (
                <div key={`${gap.object_id ?? "chain"}-${gap.label}-${i}`}>
                  {gap.label}
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
