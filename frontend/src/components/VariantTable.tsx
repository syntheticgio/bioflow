import { useEffect, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { VariantContigRow } from "../api/types";

const PAGE_SIZE = 50;

/** Debounces a fast-changing value so a text field can drive a query without
 *  firing one request per keystroke. */
function useDebounced<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(id);
  }, [value, delayMs]);
  return debounced;
}

/**
 * The complete variant table, paginated and filtered server-side against the
 * SQLite index built alongside the report -- see api.vcfStatsVariants.
 */
export function VariantTable({
  objectId,
  reportPath,
  contigs,
  filters,
  samples,
}: {
  objectId: string;
  reportPath?: string;
  contigs: VariantContigRow[];
  filters: { filter: string; count: number }[];
  samples: string[];
}) {
  const [page, setPage] = useState(0);
  const [contig, setContig] = useState("");
  const [filterValue, setFilterValue] = useState("");
  const [variantType, setVariantType] = useState("");
  const [minQualInput, setMinQualInput] = useState("");
  const [sampleIdx, setSampleIdx] = useState(0);
  // Holds the last known total across a skip_count page turn, so the row
  // count does not flash to nothing while only the page changed.
  const [lastTotal, setLastTotal] = useState<number | null>(null);

  const minQual = useDebounced(minQualInput, 300);

  // Any real filter change starts over at page 0; only the page itself
  // should leave it alone.
  useEffect(() => {
    setPage(0);
  }, [contig, filterValue, variantType, minQual]);

  const parsedMinQual = minQual.trim() === "" ? undefined : Number(minQual);
  const minQualValid = parsedMinQual == null || !Number.isNaN(parsedMinQual);

  const { data, isLoading } = useQuery({
    queryKey: [
      "vcfstats",
      "variants",
      objectId,
      page,
      contig,
      filterValue,
      variantType,
      minQualValid ? parsedMinQual : undefined,
    ],
    queryFn: () =>
      api.vcfStatsVariants(objectId, {
        offset: page * PAGE_SIZE,
        limit: PAGE_SIZE,
        contig: contig || undefined,
        filterValue: filterValue || undefined,
        variantType: variantType || undefined,
        minQual: minQualValid ? parsedMinQual : undefined,
        skipCount: page > 0,
      }),
    placeholderData: keepPreviousData,
    enabled: minQualValid,
  });

  useEffect(() => {
    if (data?.total != null) setLastTotal(data.total);
  }, [data?.total]);

  const total = data?.total ?? lastTotal;
  const rows = data?.rows ?? [];
  const hasNext = rows.length === PAGE_SIZE;

  return (
    <div className="section">
      <div
        className="section-title"
        style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}
      >
        <span>Variants</span>
        {reportPath && (
          <a
            href={api.vcfStatsDownloadUrl(objectId, reportPath)}
            style={{ marginLeft: "auto", fontSize: 11 }}
          >
            Download TSV
          </a>
        )}
      </div>

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
          <select value={contig} onChange={(e) => setContig(e.target.value)}>
            <option value="">All contigs</option>
            {contigs.map((c) => (
              <option key={c.contig} value={c.contig}>
                {c.contig} ({c.variants.toLocaleString()})
              </option>
            ))}
          </select>
        </label>

        <label style={{ display: "flex", flexDirection: "column", gap: 3 }}>
          <span style={{ color: "var(--text-faint)" }}>Filter</span>
          <select value={filterValue} onChange={(e) => setFilterValue(e.target.value)}>
            <option value="">All</option>
            {filters.map((f) => (
              <option key={f.filter} value={f.filter}>
                {f.filter === "." ? "(none)" : f.filter} ({f.count.toLocaleString()})
              </option>
            ))}
          </select>
        </label>

        <label style={{ display: "flex", flexDirection: "column", gap: 3 }}>
          <span style={{ color: "var(--text-faint)" }}>Type</span>
          <select value={variantType} onChange={(e) => setVariantType(e.target.value)}>
            <option value="">All</option>
            <option value="snp">SNPs</option>
            <option value="indel">Indels</option>
          </select>
        </label>

        <label style={{ display: "flex", flexDirection: "column", gap: 3 }}>
          <span style={{ color: "var(--text-faint)" }}>Min QUAL</span>
          <input
            type="text"
            inputMode="decimal"
            value={minQualInput}
            onChange={(e) => setMinQualInput(e.target.value)}
            placeholder="any"
            style={{ width: 80 }}
          />
        </label>

        {samples.length > 1 && (
          <label style={{ display: "flex", flexDirection: "column", gap: 3 }}>
            <span style={{ color: "var(--text-faint)" }}>Sample</span>
            <select
              value={sampleIdx}
              onChange={(e) => setSampleIdx(Number(e.target.value))}
            >
              {samples.map((s, i) => (
                <option key={s} value={i}>
                  {s}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>

      {isLoading && !data ? (
        <div style={{ color: "var(--text-faint)", fontSize: 12 }}>Loading…</div>
      ) : rows.length === 0 ? (
        <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
          No variants match these filters.
        </div>
      ) : (
        <>
          <table className="trim-table">
            <thead>
              <tr>
                <th>Chrom</th>
                <th style={{ textAlign: "right" }}>Pos</th>
                <th>Ref</th>
                <th>Alt</th>
                <th style={{ textAlign: "right" }}>Qual</th>
                <th>Filter</th>
                <th style={{ textAlign: "right" }}>Depth</th>
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
                  <td className="mono">
                    <Truncated value={row.ref} />
                  </td>
                  <td className="mono">
                    <Truncated value={row.alt} />
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
                  <td style={{ textAlign: "right" }}>{row.dp == null ? "—" : row.dp}</td>
                  <td className="mono">{genotypeFor(row.gt, sampleIdx)}</td>
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
                ? `${total.toLocaleString()} variant${total === 1 ? "" : "s"}`
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

/** REF/ALT can run 30+ bases for an indel; truncate so one long allele does
 *  not blow out the column. */
function Truncated({ value }: { value: string }) {
  if (value.length <= 12) return <>{value}</>;
  return <span title={value}>{value.slice(0, 12)}…</span>;
}

/** `gt` holds one genotype per sample, tab-separated. Falls back to the whole
 *  string when the index is out of range or there was never more than one. */
function genotypeFor(gt: string, sampleIdx: number): string {
  const parts = gt.split("\t");
  return parts[sampleIdx] ?? gt;
}
