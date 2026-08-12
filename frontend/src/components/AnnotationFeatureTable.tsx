import { useEffect, useMemo, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { AnnotationFeature, AnnotationGene, AnnotationStatsFacts } from "../api/types";
import { useDebounced } from "../lib/useDebounced";
import { TabPanel, Tabs } from "./Tabs";

const PAGE_SIZE = 100;

/** Mirrors the backend's annotation_hierarchy.DEPTH_CAP. A cyclic or
 *  pathologically deep parent chain must not make the client recurse
 *  forever fetching children -- once a row's own depth reaches the cap, it
 *  is rendered as a leaf regardless of has_children. */
const DEPTH_CAP = 100;

type View = "genes" | "all" | "unresolved";

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
 *
 * A three-way view toggle sits above the table: Genes (a per-gene summary,
 * the default), All records (the original flat/hierarchical feature table),
 * and Unresolved (rows whose parent reference didn't resolve cleanly).
 */
export function AnnotationFeatureTable({
  objectId,
  facts,
}: {
  objectId: string;
  facts: AnnotationStatsFacts;
}) {
  const [view, setView] = useState<View>("genes");
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

  // Any real filter change, or a switch between views, starts over at page 0
  // and drops expansion state -- a child row expanded under the old filters
  // (or a different view entirely) must not survive into a differently
  // scoped result set, since its parent may no longer even be on the page.
  // VariantTable.tsx only needs the page reset; the expansion clear is new
  // here because it's the only one of these two tables with hierarchy.
  useEffect(() => {
    setPage(0);
    setExpanded(new Set());
  }, [view, contig, featureType, biotype, strand, nameQuery, locus]);

  // Effective contig: an active locus jump overrides the contig dropdown.
  const effectiveContig = locus?.contig || contig || undefined;

  const genesQuery = useQuery({
    queryKey: ["annotationstats", "genes", objectId, page],
    queryFn: () => api.annotationGenes(objectId, page * PAGE_SIZE, PAGE_SIZE, page > 0),
    enabled: view === "genes",
    placeholderData: keepPreviousData,
  });

  const featuresQuery = useQuery({
    queryKey: [
      "annotationstats",
      "features",
      objectId,
      page,
      view,
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
        view: view === "unresolved" ? "unresolved" : undefined,
      }),
    enabled: view !== "genes",
    placeholderData: keepPreviousData,
  });

  const isGenesView = view === "genes";
  const data = isGenesView ? genesQuery.data : featuresQuery.data;
  const isLoading = isGenesView ? genesQuery.isLoading : featuresQuery.isLoading;

  const [lastTotal, setLastTotal] = useState<number | null>(null);
  useEffect(() => {
    if (data?.total != null) setLastTotal(data.total);
  }, [data?.total]);

  const total = data?.total ?? lastTotal;
  const geneRows = genesQuery.data?.rows ?? [];
  const featureRows = featuresQuery.data?.rows ?? [];
  const rowCount = isGenesView ? geneRows.length : featureRows.length;
  const hasNext = rowCount === PAGE_SIZE;

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

  const unresolvedCount = facts.annotation_unresolved_count;

  return (
    <div className="section">
      <div className="section-title">Features</div>

      <Tabs
        idPrefix="annotation-view"
        active={view}
        onChange={(id) => setView(id as View)}
        tabs={[
          { id: "genes", label: "Genes" },
          { id: "all", label: "All records" },
          {
            id: "unresolved",
            label: "Unresolved",
            hint: unresolvedCount ? String(unresolvedCount) : undefined,
          },
        ]}
      />

      <TabPanel id={view} idPrefix="annotation-view">
        {view !== "genes" && (
          <div
            style={{
              display: "flex",
              gap: 12,
              flexWrap: "wrap",
              alignItems: "flex-end",
              margin: "10px 0",
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
        )}

        {view !== "genes" && featureType && (
          <div style={{ color: "var(--text-faint)", fontSize: 11, marginBottom: 6 }}>
            Showing every {featureType} feature, including those nested under a parent.
          </div>
        )}

        {isGenesView && genesQuery.data?.mode === "fallback" && (
          <div style={{ color: "var(--warn)", fontSize: 11, marginBottom: 6 }}>
            No gene records in this file; showing top-level features.
          </div>
        )}

        {isLoading && !data ? (
          <div style={{ color: "var(--text-faint)", fontSize: 12 }}>Loading…</div>
        ) : rowCount === 0 ? (
          <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
            {isGenesView ? "No genes found." : "No features match these filters."}
          </div>
        ) : isGenesView ? (
          <table className="trim-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Type</th>
                <th>Contig</th>
                <th style={{ textAlign: "right" }}>Start</th>
                <th style={{ textAlign: "right" }}>End</th>
                <th style={{ textAlign: "right" }}>Span</th>
                <th style={{ textAlign: "right" }}>Children</th>
                <th style={{ textAlign: "right" }}>Descendants</th>
                <th>Strand</th>
                <th>Biotype</th>
              </tr>
            </thead>
            <tbody>
              {geneRows.map((gene, i) => (
                <GeneRow
                  key={gene.feature_id ?? `${gene.contig}:${gene.start}:${gene.end}:${i}`}
                  gene={gene}
                />
              ))}
            </tbody>
          </table>
        ) : (
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
              {featureRows.map((row, i) => (
                <FeatureRow
                  key={row.feature_id ?? `${row.contig}:${row.start}:${row.end}:${i}`}
                  objectId={objectId}
                  row={row}
                  expandedIds={expanded}
                  onToggle={toggleExpanded}
                  depth={0}
                  depthCap={DEPTH_CAP}
                />
              ))}
            </tbody>
          </table>
        )}

        {rowCount > 0 && (
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
                ? `${total.toLocaleString()} ${isGenesView ? "gene" : "feature"}${total === 1 ? "" : "s"}`
                : `Showing ${rowCount}`}
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
        )}
      </TabPanel>
    </div>
  );
}

/** One row of the Genes view -- a flat summary row, no expand/collapse. */
function GeneRow({ gene }: { gene: AnnotationGene }) {
  const span = gene.span_end - gene.span_start + 1;
  return (
    <tr>
      <td className="mono">{gene.name ?? "—"}</td>
      <td>{gene.type ?? "—"}</td>
      <td className="mono">{gene.contig}</td>
      <td style={{ textAlign: "right" }} className="mono">
        {gene.start.toLocaleString()}
      </td>
      <td style={{ textAlign: "right" }} className="mono">
        {gene.end.toLocaleString()}
      </td>
      <td style={{ textAlign: "right" }}>{span.toLocaleString()}</td>
      <td style={{ textAlign: "right" }}>{gene.child_count.toLocaleString()}</td>
      <td style={{ textAlign: "right" }}>{gene.descendant_count.toLocaleString()}</td>
      <td>{gene.strand ?? "—"}</td>
      <td>{gene.biotype ?? "—"}</td>
    </tr>
  );
}

/** One feature row, plus its expanded children (fetched lazily and cached
 *  per-parent by React Query's queryKey) rendered directly beneath it.
 *  `expandedIds`/`onToggle` are threaded down whole rather than resolved to a
 *  single boolean, so a grandchild row can toggle its own expansion using the
 *  same shared set the top-level table owns -- GFF/GTF hierarchies can run
 *  deeper than one level (e.g. gene -> mRNA -> exon).
 *
 *  `depth` doubles as a recursion guard: once a row's own depth reaches
 *  `depthCap`, it renders as a leaf -- no expand chevron, no children query
 *  -- regardless of has_children. Past that point the backend itself no
 *  longer trusts the parent chain (cyclic or otherwise unresolved), so
 *  recursing further client-side would just be following data the server
 *  has already given up on. `depthCap` starts at the module's own DEPTH_CAP
 *  for the top-level rows, then each row passes its children the cap its
 *  own children query actually echoed back (falling back to the constant
 *  while that query hasn't resolved yet) -- so the client tracks the
 *  server's real bound rather than hardcoding a second copy of it. */
function FeatureRow({
  objectId,
  row,
  expandedIds,
  onToggle,
  depth,
  depthCap,
}: {
  objectId: string;
  row: AnnotationFeature;
  expandedIds: Set<string>;
  onToggle: (featureId: string) => void;
  depth: number;
  depthCap: number;
}) {
  const expanded = !!row.feature_id && expandedIds.has(row.feature_id);
  const expandable = row.has_children && depth < depthCap;

  const { data: childData } = useQuery({
    queryKey: ["annotationstats", "children", objectId, row.feature_id],
    queryFn: () => api.annotationChildren(objectId, row.feature_id as string),
    enabled: expanded && !!row.feature_id && depth < depthCap,
  });

  const childDepthCap = childData?.depth_cap ?? DEPTH_CAP;

  const length = row.end - row.start + 1;
  const indent = 8 + depth * 16;
  const flagged = row.parent_status !== "root" && row.parent_status !== "resolved";

  return (
    <>
      <tr>
        <td style={{ paddingLeft: indent }}>
          {expandable && (
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
        <td className="mono">
          {row.name ?? "—"}
          {flagged && (
            <span style={{ color: "var(--warn)", fontSize: 11, marginLeft: 6 }}>
              ({row.parent_status}
              {row.parent ? ` → ${row.parent}` : ""})
            </span>
          )}
        </td>
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
            depthCap={childDepthCap}
          />
        ))}
    </>
  );
}
