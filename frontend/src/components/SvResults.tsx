import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { ObjectDetail as ObjectDetailData } from "../api/types";
import { InfoMarker } from "./InfoMarker";
import { Stat } from "./Stat";
import { SvLengthChart } from "./SvLengthChart";
import { SvTable } from "./SvTable";

/**
 * What structural variant calling produced (Sniffles2 for long reads, Delly
 * for short reads): the type breakdown, the log-binned length histogram, the
 * complete filterable SV table, and the VCF+TBI download.
 *
 * Unlike VariantResults there is no on-demand "compute results" step --
 * `call_structural_variants` builds the SQLite index in the same job that
 * runs the caller (see sv_handlers.py), so the summary and table are simply
 * fetched once the VCF object exists. A 404 from either read endpoint means
 * the SV calling job has not finished yet, or failed after producing a VCF
 * with no index -- both render as "not ready" rather than an error, since
 * there is nothing here for the user to retry from this view.
 */
export function SvResults({ obj }: { obj: ObjectDetailData }) {
  const { data: summary, isLoading: summaryLoading, isError: summaryError } = useQuery({
    queryKey: ["structural_variants", "summary", obj.id],
    queryFn: () => api.structuralVariantSummary(obj.id),
    retry: false,
  });

  // Siblings in the project, so the VCF's own .tbi sidecar can be offered
  // for download without a dedicated endpoint -- the same lookup
  // DerivedFiles.tsx uses for "related files" generally.
  const { data: siblings = [] } = useQuery({
    queryKey: ["objects", obj.project_id],
    queryFn: () => api.listObjects(obj.project_id),
  });

  const tbi = siblings.find(
    (o) => o.sidecar_of === obj.id && o.sidecar_role === "tbi",
  );

  const calledBy =
    typeof obj.facts.variants_called_by === "string"
      ? obj.facts.variants_called_by +
        (typeof obj.facts.variant_caller_version === "string"
          ? ` ${obj.facts.variant_caller_version}`
          : "")
      : null;

  if (summaryLoading) {
    return (
      <div className="section-note" style={{ color: "var(--text-faint)" }}>
        Loading structural variant results…
      </div>
    );
  }

  if (summaryError || !summary) {
    return (
      <div className="section-note">
        No structural variant results yet. Structural variant calling builds
        its results as part of the run, so this appears once the structural
        variant calling job has finished.
      </div>
    );
  }

  const totalCalls = Object.values(summary.type_counts).reduce((a, b) => a + b, 0);

  return (
    <>
      <div className="qc-provenance">
        {[calledBy ? `called by ${calledBy}` : null].filter(Boolean).join(" · ")}
      </div>

      {/* Decision 6: the VCF and its .tbi are offered together. A .vcf.gz
          without its tabix index is not a track IGV or JBrowse can load, so
          this never renders the VCF link alone. */}
      <div className="section">
        <div className="section-title">Download</div>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
          <a className="btn-text" href={api.objectDownloadUrl(obj.id)}>
            Download VCF ({obj.name})
          </a>
          {tbi ? (
            <a className="btn-text" href={api.objectDownloadUrl(tbi.id)}>
              Download index ({tbi.name})
            </a>
          ) : (
            <span style={{ color: "var(--text-faint)", fontSize: 12 }}>
              Index (.tbi) not available yet
            </span>
          )}
        </div>
      </div>

      {totalCalls === 0 ? (
        <div className="section">
          <div className="section-note">
            No structural variants were called. For a clean sample against a
            close reference this is a normal outcome, not a failure.
          </div>
        </div>
      ) : (
        <>
          <div className="section">
            <div style={{ display: "flex", gap: 20, flexWrap: "wrap" }}>
              <Stat
                label="Structural variants"
                metric="ui.sv_total"
                value={totalCalls.toLocaleString()}
              />
              {Object.entries(summary.type_counts)
                .sort(([, a], [, b]) => b - a)
                .map(([type, count]) => (
                  <Stat
                    key={type}
                    label={type}
                    metric="ui.sv_type_count"
                    value={count.toLocaleString()}
                  />
                ))}
            </div>
          </div>

          <div className="section">
            <div className="section-title">
              Length distribution
              <InfoMarker metric="ui.chart_sv_length" />
            </div>
            <SvLengthChart buckets={summary.length_histogram} />
          </div>

          <SvTable
            objectId={obj.id}
            typeCounts={summary.type_counts}
            samples={summary.samples ?? []}
          />
        </>
      )}
    </>
  );
}
