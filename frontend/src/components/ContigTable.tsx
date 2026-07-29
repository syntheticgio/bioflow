import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";

const PAGE_SIZE = 25;

/**
 * The complete per-contig table, paginated server-side against the TSV
 * report -- not the capped `bam_stats_contigs_top` in facts, which only
 * covers the visualization's top-N slice.
 */
export function ContigTable({
  objectId,
  reportPath,
}: {
  objectId: string;
  reportPath: string;
}) {
  const [page, setPage] = useState(0);

  const { data, isLoading } = useQuery({
    queryKey: ["bamstats", "contigs", objectId, reportPath, page],
    queryFn: () => api.bamStatsContigs(objectId, reportPath, page * PAGE_SIZE, PAGE_SIZE),
  });

  const totalPages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1;

  return (
    <div className="section">
      <div
        className="section-title"
        style={{ display: "flex", alignItems: "center", gap: 8 }}
      >
        <span>Per-contig coverage</span>
        <a
          href={api.bamStatsDownloadUrl(objectId, reportPath)}
          style={{ marginLeft: "auto", fontSize: 11 }}
        >
          Download TSV
        </a>
      </div>

      {isLoading || !data ? (
        <div style={{ color: "var(--text-faint)", fontSize: 12 }}>Loading…</div>
      ) : (
        <>
          <table className="trim-table">
            <thead>
              <tr>
                <th>Contig</th>
                <th>Length</th>
                <th>Reads</th>
                <th>Coverage</th>
                <th>Mean depth</th>
                <th>Mean MAPQ</th>
              </tr>
            </thead>
            <tbody>
              {data.rows.map((row) => (
                <tr key={row.contig}>
                  <td className="mono">{row.contig}</td>
                  <td>{row.length.toLocaleString()}</td>
                  <td>{row.reads.toLocaleString()}</td>
                  <td>{row.coverage_pct.toFixed(1)}%</td>
                  <td>{row.mean_depth.toFixed(1)}×</td>
                  <td>{row.mean_mapq.toFixed(1)}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              marginTop: 8,
              fontSize: 11,
              color: "var(--text-faint)",
            }}
          >
            <span>
              {data.total.toLocaleString()} contig{data.total === 1 ? "" : "s"}
            </span>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <button
                type="button"
                className="btn"
                style={{ padding: "1px 8px", fontSize: 11 }}
                onClick={() => setPage((p) => Math.max(0, p - 1))}
                disabled={page === 0}
              >
                Prev
              </button>
              <span>
                Page {page + 1} of {totalPages}
              </span>
              <button
                type="button"
                className="btn"
                style={{ padding: "1px 8px", fontSize: 11 }}
                onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
                disabled={page + 1 >= totalPages}
              >
                Next
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
