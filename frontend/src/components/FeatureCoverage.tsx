import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { InfoMarker } from "./InfoMarker";

/**
 * The per-feature coverage table: one row per feature in the project's
 * annotation, breadth-of-coverage against a BAM computed by `bedtools
 * coverage`. Unlike ContigTable's per-contig report, there is no pagination
 * here -- the endpoint returns the whole (server-capped) report in one
 * shot, so this is a single fetch with no page state.
 */
export function FeatureCoverage({ objectId }: { objectId: string }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["feature-coverage", objectId],
    queryFn: () => api.featureCoverageReport(objectId),
  });

  return (
    <div className="section">
      <div
        className="section-title"
        style={{ display: "flex", alignItems: "center", gap: 8 }}
      >
        <span>Per-feature coverage</span>
        <InfoMarker metric="ui.feature_coverage_table" />
      </div>

      {isLoading ? (
        <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
          Loading per-feature coverage…
        </div>
      ) : isError || !data ? (
        <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
          Couldn't load the per-feature coverage report.
        </div>
      ) : (
        <>
          <div
            style={{ color: "var(--text-faint)", fontSize: 12, marginBottom: 8 }}
          >
            {data.feature_count.toLocaleString()} feature
            {data.feature_count === 1 ? "" : "s"} ·{" "}
            {data.features_zero_coverage.toLocaleString()} at zero coverage ·
            median breadth {(data.median_breadth * 100).toFixed(1)}%
            {data.truncated && (
              <>
                {" "}
                · showing the first {data.features.length.toLocaleString()} of{" "}
                {data.feature_count.toLocaleString()} features
              </>
            )}
          </div>

          <table className="trim-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Type</th>
                <th>Location</th>
                <th>Reads</th>
                <th>Breadth</th>
              </tr>
            </thead>
            <tbody>
              {data.features.map((row, i) => (
                <tr key={`${row.seq_id}:${row.start}-${row.end}:${i}`}>
                  <td className="mono">{row.name}</td>
                  <td>{row.type}</td>
                  <td className="mono">
                    {row.seq_id}:{row.start.toLocaleString()}-
                    {row.end.toLocaleString()}
                  </td>
                  <td>{row.read_count.toLocaleString()}</td>
                  <td>{(row.breadth * 100).toFixed(1)}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}
