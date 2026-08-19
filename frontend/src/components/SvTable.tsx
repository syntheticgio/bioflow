import { useEffect, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { useDebounced } from "../lib/useDebounced";

const PAGE_SIZE = 50;

/**
 * The complete structural variant table, paginated and filtered server-side
 * against the SQLite index built alongside the SV VCF -- see
 * api.structuralVariants. Mirrors VariantTable's shape.
 */
export function SvTable({
  objectId,
  typeCounts,
}: {
  objectId: string;
  typeCounts: Record<string, number>;
}) {
  const [page, setPage] = useState(0);
  const [contig, setContig] = useState("");
  const [svtype, setSvtype] = useState("");
  const [filterValue, setFilterValue] = useState("");
  const [minLengthInput, setMinLengthInput] = useState("");
  const [maxLengthInput, setMaxLengthInput] = useState("");
  // Holds the last known total across a skip_count page turn, so the row
  // count does not flash to nothing while only the page changed.
  const [lastTotal, setLastTotal] = useState<number | null>(null);

  const minLength = useDebounced(minLengthInput, 300);
  const maxLength = useDebounced(maxLengthInput, 300);

  // Any real filter change starts over at page 0; only the page itself
  // should leave it alone.
  useEffect(() => {
    setPage(0);
  }, [contig, svtype, filterValue, minLength, maxLength]);

  const parsedMinLength = minLength.trim() === "" ? undefined : Number(minLength);
  const parsedMaxLength = maxLength.trim() === "" ? undefined : Number(maxLength);
  const lengthValid =
    (parsedMinLength == null || !Number.isNaN(parsedMinLength)) &&
    (parsedMaxLength == null || !Number.isNaN(parsedMaxLength));

  const { data, isLoading } = useQuery({
    queryKey: [
      "structural_variants",
      "svs",
      objectId,
      page,
      contig,
      svtype,
      filterValue,
      lengthValid ? parsedMinLength : undefined,
      lengthValid ? parsedMaxLength : undefined,
    ],
    queryFn: () =>
      api.structuralVariants(objectId, {
        offset: page * PAGE_SIZE,
        limit: PAGE_SIZE,
        contig: contig || undefined,
        svtype: svtype || undefined,
        filterValue: filterValue || undefined,
        minLength: lengthValid ? parsedMinLength : undefined,
        maxLength: lengthValid ? parsedMaxLength : undefined,
        skipCount: page > 0,
      }),
    placeholderData: keepPreviousData,
    enabled: lengthValid,
  });

  useEffect(() => {
    if (data?.total != null) setLastTotal(data.total);
  }, [data?.total]);

  const total = data?.total ?? lastTotal;
  const rows = data?.rows ?? [];
  const hasNext = rows.length === PAGE_SIZE;

  const svtypes = Object.keys(typeCounts).sort();

  return (
    <div className="section">
      <div className="section-title">Structural variants</div>

      <div
        style={{
          display: "flex",
          gap: 12,
          flexWrap: "wrap",
          alignItems: "flex-end",
          marginBottom: 10,
          fontSize: 12,
        }}
      >
        <label style={{ display: "flex", flexDirection: "column", gap: 3 }}>
          <span style={{ color: "var(--text-faint)" }}>Contig</span>
          <input
            type="text"
            value={contig}
            onChange={(e) => setContig(e.target.value)}
            placeholder="any"
            style={{ width: 100 }}
          />
        </label>

        <label style={{ display: "flex", flexDirection: "column", gap: 3 }}>
          <span style={{ color: "var(--text-faint)" }}>Type</span>
          <select value={svtype} onChange={(e) => setSvtype(e.target.value)}>
            <option value="">All</option>
            {svtypes.map((t) => (
              <option key={t} value={t}>
                {t} ({typeCounts[t].toLocaleString()})
              </option>
            ))}
          </select>
        </label>

        <label style={{ display: "flex", flexDirection: "column", gap: 3 }}>
          <span style={{ color: "var(--text-faint)" }}>Filter</span>
          <select value={filterValue} onChange={(e) => setFilterValue(e.target.value)}>
            <option value="">All</option>
            <option value="PASS">PASS</option>
          </select>
        </label>

        <label style={{ display: "flex", flexDirection: "column", gap: 3 }}>
          <span style={{ color: "var(--text-faint)" }}>Min length</span>
          <input
            type="text"
            inputMode="numeric"
            value={minLengthInput}
            onChange={(e) => setMinLengthInput(e.target.value)}
            placeholder="any"
            style={{ width: 80 }}
          />
        </label>

        <label style={{ display: "flex", flexDirection: "column", gap: 3 }}>
          <span style={{ color: "var(--text-faint)" }}>Max length</span>
          <input
            type="text"
            inputMode="numeric"
            value={maxLengthInput}
            onChange={(e) => setMaxLengthInput(e.target.value)}
            placeholder="any"
            style={{ width: 80 }}
          />
        </label>
      </div>

      {isLoading && !data ? (
        <div style={{ color: "var(--text-faint)", fontSize: 12 }}>Loading…</div>
      ) : rows.length === 0 ? (
        <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
          No structural variants match these filters.
        </div>
      ) : (
        <>
          <table className="trim-table">
            <thead>
              <tr>
                <th>Chrom</th>
                <th style={{ textAlign: "right" }}>Pos</th>
                <th style={{ textAlign: "right" }}>End</th>
                <th>Type</th>
                <th style={{ textAlign: "right" }}>Length</th>
                <th style={{ textAlign: "right" }}>Qual</th>
                <th>Filter</th>
                <th style={{ textAlign: "right" }}>Support</th>
                <th>Genotype</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row, i) => (
                <tr key={`${row.chrom}:${row.pos}:${i}`}>
                  <td className="mono">{row.chrom}</td>
                  <td style={{ textAlign: "right" }} className="mono">
                    {row.pos.toLocaleString()}
                  </td>
                  <td style={{ textAlign: "right" }} className="mono">
                    {row.end == null ? "—" : row.end.toLocaleString()}
                  </td>
                  <td className="mono">{row.svtype}</td>
                  <td style={{ textAlign: "right" }}>
                    {row.svlen == null ? "—" : row.svlen.toLocaleString()}
                  </td>
                  <td style={{ textAlign: "right" }}>
                    {row.qual == null ? "—" : row.qual.toFixed(1)}
                  </td>
                  <td>
                    {row.filter === "PASS" ? (
                      <span className="badge ready">PASS</span>
                    ) : (
                      row.filter
                    )}
                  </td>
                  <td style={{ textAlign: "right" }}>
                    {row.support == null ? "—" : row.support}
                  </td>
                  <td className="mono">{row.gt}</td>
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
              {total != null
                ? `${total.toLocaleString()} structural variant${total === 1 ? "" : "s"}`
                : `Showing ${rows.length}`}
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
              <span>Page {page + 1}</span>
              <button
                type="button"
                className="btn"
                style={{ padding: "1px 8px", fontSize: 11 }}
                onClick={() => setPage((p) => p + 1)}
                disabled={!hasNext}
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
