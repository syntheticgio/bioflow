import { useEffect, useMemo, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { AnnotationFeature, AnnotationStatsFacts } from "../api/types";
import { useDebounced } from "../lib/useDebounced";

const PAGE_SIZE = 100;

/** Result of parsing a locus-jump input like "chr1:1,000,000-1,050,000" or
 *  a bare "chr1". Commas and spaces inside the numbers are tolerated since
 *  that is how genome browsers commonly display coordinates. */
export function parseLocus(
  input: string,
): { contig: string; min?: number; max?: number } | null {
  const trimmed = input.trim();
  if (!trimmed) return null;

  const colonIdx = trimmed.indexOf(":");
  if (colonIdx === -1) {
    // Bare contig, no range.
    return { contig: trimmed };
  }

  const contig = trimmed.slice(0, colonIdx).trim();
  const rangePart = trimmed.slice(colonIdx + 1).trim();
  if (!contig || !rangePart) return null;

  const clean = (s: string) => s.replace(/[,\s]/g, "");
  const dashIdx = rangePart.indexOf("-");
  if (dashIdx === -1) {
    const pos = Number(clean(rangePart));
    if (Number.isNaN(pos)) return null;
    return { contig, min: pos, max: pos };
  }

  const minStr = clean(rangePart.slice(0, dashIdx));
  const maxStr = clean(rangePart.slice(dashIdx + 1));
  const min = Number(minStr);
  const max = Number(maxStr);
  if (Number.isNaN(min) || Number.isNaN(max)) return null;
  return { contig, min, max };
}

/**
 * The complete annotation feature table, paginated and filtered server-side
 * against the SQLite feature index built alongside the report -- see
 * api.annotationFeatures. Mirrors VariantTable.tsx's real conventions
 * (`section`/`section-title`/`trim-table`, `hasNext`-based paging via
 * `keepPreviousData`, `useDebounced` for the name search) rather than the
 * `filter-bar`/`data-table`/`pager` classes and server-page-count pattern
 * an earlier draft of this task assumed -- none of those exist in this
 * codebase.
 */
export function AnnotationFeatureTable({
  objectId,
  facts,
}: {
  objectId: string;
  facts: AnnotationStatsFacts;
}) {
  const [page, setPage] = useState(0);
  const [contig, setContig] = useState("");
  const [featureType, setFeatureType] = useState("");
  const [biotype, setBiotype] = useState("");
  const [strand, setStrand] = useState("");
  const [nameInput, setNameInput] = useState("");
  const [locusInput, setLocusInput] = useState("");
  const [locus, setLocus] = useState<{ contig: string; min?: number; max?: number } | null>(
    null,
  );
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const nameQuery = useDebounced(nameInput, 300);

  const contigOptions = useMemo(
    () => (facts.annotation_per_contig ?? []).map((c) => c.name),
    [facts.annotation_per_contig],
  );

  const featureTypeOptions = useMemo(
    () => Object.keys(facts.annotation_type_counts ?? {}).sort(),
    [facts.annotation_type_counts],
  );

  const biotypeOptions = useMemo(
    () => Object.keys(facts.annotation_biotype_counts ?? {}).sort(),
    [facts.annotation_biotype_counts],
  );

  // Any real filter change starts over at page 0 and drops expansion state --
  // a child row expanded under the old filters must not survive into a
  // differently-filtered result set, since its parent may no longer even be
  // on the page. VariantTable.tsx only needs the page reset; the expansion
  // clear is new here because it's the only one of these two tables with
  // hierarchy.
  useEffect(() => {
    setPage(0);
    setExpanded(new Set());
  }, [contig, featureType, biotype, strand, nameQuery, locus]);

  // Effective contig: an active locus jump overrides the contig dropdown.
  const effectiveContig = locus?.contig || contig || undefined;

  const { data, isLoading } = useQuery({
    queryKey: [
      "annotationstats",
      "features",
      objectId,
      page,
      effectiveContig,
      locus?.min,
      locus?.max,
      featureType,
      biotype,
      nameQuery,
      strand,
    ],
    queryFn: () =>
      api.annotationFeatures(objectId, {
        offset: page * PAGE_SIZE,
        limit: PAGE_SIZE,
        contig: effectiveContig,
        startMin: locus?.min,
        startMax: locus?.max,
        featureType: featureType || undefined,
        biotype: biotype || undefined,
        nameQuery: nameQuery || undefined,
        strand: strand || undefined,
        skipCount: page > 0,
      }),
    placeholderData: keepPreviousData,
  });

  const [lastTotal, setLastTotal] = useState<number | null>(null);
  useEffect(() => {
    if (data?.total != null) setLastTotal(data.total);
  }, [data?.total]);

  const total = data?.total ?? lastTotal;
  const rows = data?.rows ?? [];
  const hasNext = rows.length === PAGE_SIZE;

  function toggleExpanded(featureId: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(featureId)) next.delete(featureId);
      else next.add(featureId);
      return next;
    });
  }

  function applyLocus() {
    const parsed = parseLocus(locusInput);
    setLocus(parsed);
    if (parsed) setContig("");
  }

  function clearLocus() {
    setLocus(null);
    setLocusInput("");
  }

  return (
    <div className="section">
      <div className="section-title">Features</div>

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
          <select
            value={contig}
            onChange={(e) => {
              setContig(e.target.value);
              setLocus(null);
              setLocusInput("");
            }}
          >
            <option value="">All contigs</option>
            {contigOptions.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>

        {featureTypeOptions.length > 0 && (
          <label style={{ display: "flex", flexDirection: "column", gap: 3 }}>
            <span style={{ color: "var(--text-faint)" }}>Type</span>
            <select value={featureType} onChange={(e) => setFeatureType(e.target.value)}>
              <option value="">All</option>
              {featureTypeOptions.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </label>
        )}

        {biotypeOptions.length > 0 && (
          <label style={{ display: "flex", flexDirection: "column", gap: 3 }}>
            <span style={{ color: "var(--text-faint)" }}>Biotype</span>
            <select value={biotype} onChange={(e) => setBiotype(e.target.value)}>
              <option value="">All</option>
              {biotypeOptions.map((b) => (
                <option key={b} value={b}>
                  {b}
                </option>
              ))}
            </select>
          </label>
        )}

        <label style={{ display: "flex", flexDirection: "column", gap: 3 }}>
          <span style={{ color: "var(--text-faint)" }}>Strand</span>
          <select value={strand} onChange={(e) => setStrand(e.target.value)}>
            <option value="">All</option>
            <option value="+">Forward (+)</option>
            <option value="-">Reverse (−)</option>
          </select>
        </label>

        <label style={{ display: "flex", flexDirection: "column", gap: 3 }}>
          <span style={{ color: "var(--text-faint)" }}>Name</span>
          <input
            type="search"
            value={nameInput}
            onChange={(e) => setNameInput(e.target.value)}
            placeholder="search"
            style={{ width: 120 }}
          />
        </label>

        <label style={{ display: "flex", flexDirection: "column", gap: 3 }}>
          <span style={{ color: "var(--text-faint)" }}>Locus</span>
          <input
            type="text"
            value={locusInput}
            onChange={(e) => setLocusInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") applyLocus();
            }}
            onBlur={applyLocus}
            placeholder="chr1:1,000-2,000"
            style={{ width: 160 }}
          />
        </label>

        {locus && (
          <button
            type="button"
            className="btn"
            style={{ padding: "1px 8px", fontSize: 11 }}
            onClick={clearLocus}
          >
            Clear locus
          </button>
        )}
      </div>

      {featureType && (
        <div style={{ color: "var(--text-faint)", fontSize: 11, marginBottom: 6 }}>
          Showing every {featureType} feature, including those nested under a parent.
        </div>
      )}

      {isLoading && !data ? (
        <div style={{ color: "var(--text-faint)", fontSize: 12 }}>Loading…</div>
      ) : rows.length === 0 ? (
        <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
          No features match these filters.
        </div>
      ) : (
        <>
          <table className="trim-table">
            <thead>
              <tr>
                <th />
                <th>Name</th>
                <th>Type</th>
                <th>Contig</th>
                <th style={{ textAlign: "right" }}>Start</th>
                <th style={{ textAlign: "right" }}>End</th>
                <th style={{ textAlign: "right" }}>Length</th>
                <th>Strand</th>
                <th>Biotype</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row, i) => (
                <FeatureRow
                  key={row.feature_id ?? `${row.contig}:${row.start}:${row.end}:${i}`}
                  objectId={objectId}
                  row={row}
                  expandedIds={expanded}
                  onToggle={toggleExpanded}
                  depth={0}
                />
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
                ? `${total.toLocaleString()} feature${total === 1 ? "" : "s"}`
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

/** One feature row, plus its expanded children (fetched lazily and cached
 *  per-parent by React Query's queryKey) rendered directly beneath it.
 *  `expandedIds`/`onToggle` are threaded down whole rather than resolved to a
 *  single boolean, so a grandchild row can toggle its own expansion using the
 *  same shared set the top-level table owns -- GFF/GTF hierarchies can run
 *  deeper than one level (e.g. gene -> mRNA -> exon). */
function FeatureRow({
  objectId,
  row,
  expandedIds,
  onToggle,
  depth,
}: {
  objectId: string;
  row: AnnotationFeature;
  expandedIds: Set<string>;
  onToggle: (featureId: string) => void;
  depth: number;
}) {
  const expanded = !!row.feature_id && expandedIds.has(row.feature_id);

  const { data: childData } = useQuery({
    queryKey: ["annotationstats", "children", objectId, row.feature_id],
    queryFn: () => api.annotationChildren(objectId, row.feature_id as string),
    enabled: expanded && !!row.feature_id,
  });

  const length = row.end - row.start + 1;
  const indent = 8 + depth * 16;

  return (
    <>
      <tr>
        <td style={{ paddingLeft: indent }}>
          {row.has_children && (
            <button
              type="button"
              className="btn"
              style={{ padding: "0 4px", fontSize: 11 }}
              onClick={() => row.feature_id && onToggle(row.feature_id)}
              aria-label={expanded ? "Collapse" : "Expand"}
            >
              {expanded ? "▾" : "▸"}
            </button>
          )}
        </td>
        <td className="mono">{row.name ?? "—"}</td>
        <td>{row.type ?? "—"}</td>
        <td className="mono">{row.contig}</td>
        <td style={{ textAlign: "right" }} className="mono">
          {row.start.toLocaleString()}
        </td>
        <td style={{ textAlign: "right" }} className="mono">
          {row.end.toLocaleString()}
        </td>
        <td style={{ textAlign: "right" }}>{length.toLocaleString()}</td>
        <td>{row.strand ?? "—"}</td>
        <td>{row.biotype ?? "—"}</td>
      </tr>
      {expanded &&
        (childData?.rows ?? []).map((child, i) => (
          <FeatureRow
            key={child.feature_id ?? `${child.contig}:${child.start}:${child.end}:${i}`}
            objectId={objectId}
            row={child}
            expandedIds={expandedIds}
            onToggle={onToggle}
            depth={depth + 1}
          />
        ))}
    </>
  );
}
