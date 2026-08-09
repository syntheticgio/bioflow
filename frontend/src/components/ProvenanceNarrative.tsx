import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../api/client";
import { notify } from "../stores/messageStore";
import { Markdown } from "./Markdown";

/**
 * The methods report for one file.
 *
 * The structured report is the deliverable and renders unconditionally. The
 * prose button is a second rendering of the same facts, and it is fetched
 * only on click -- it costs a model call, and most opens of this panel do
 * not want one.
 */
export function ProvenanceNarrative({ objectId }: { objectId: string }) {
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

  return (
    <div className="section">
      <div className="section-title">
        Provenance
        <span className="badge" style={{ marginLeft: 8 }}>
          {data.gap_count === 0
            ? "All facts recorded"
            : `${data.gap_count} facts not recorded`}
        </span>
      </div>

      {/* The structured report and its prose rendering are the same facts
          twice -- the theme sets them side by side where there is width for
          it, and they stack on their own when there is not. */}
      <div className="detail-columns">
        <div>
          <Markdown source={data.markdown} />

          <div className="detail-actions">
            <button
              type="button"
              className="btn-text"
              onClick={() => copy(data.markdown, "report")}
            >
              {copied === "report" ? "Copied" : "Copy report"}
            </button>
            <button
              type="button"
              className="btn"
              onClick={() => prose.mutate()}
              disabled={prose.isPending}
            >
              {prose.isPending ? "Generating…" : "Generate prose"}
            </button>
          </div>
        </div>

        {(proseText || prose.data?.unavailable_reason) && (
          <div>
            {proseText && (
              <>
                <div className="section-title">Narrative</div>
                <p className="ai-summary-body">{proseText}</p>
                <div className="detail-actions">
                  <button
                    type="button"
                    className="btn-text"
                    onClick={() => copy(proseText, "prose")}
                  >
                    {copied === "prose" ? "Copied" : "Copy paragraph"}
                  </button>
                </div>
              </>
            )}

            {prose.data?.unavailable_reason && (
              <div className="section-note">{prose.data.unavailable_reason}</div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
