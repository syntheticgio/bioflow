import { useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { DeRow, ObjectDetail as ObjectDetailData } from "../api/types";
import { AiSummary } from "./AiSummary";
import {
  MAPlot,
  SampleCorrelationHeatmap,
  SamplePcaPlot,
  VolcanoPlot,
  type PcaPoint,
  type SampleCorrelation,
} from "./ExpressionCharts";
import { InfoMarker } from "./InfoMarker";
import { NodeSelector } from "./NodeSelector";

const PAGE_SIZE = 50;

// How many genes the plots are drawn from. The table pages; the plots need the
// whole distribution or they misrepresent it, and 20k circles is already more
// than an SVG wants. Genes are fetched sorted by padj, so a truncated fetch
// keeps everything significant and drops from the uninteresting tail -- the
// one direction where losing points does not change what the plot says.
const PLOT_LIMIT = 20000;

type DeFacts = {
  contrast_test?: string;
  contrast_reference?: string;
  alpha?: number;
  samples?: number;
  samples_by_condition?: Record<string, number>;
  genes_in_matrix?: number;
  genes_tested?: number;
  significant_genes?: number;
  significant_up?: number;
  significant_down?: number;
  sample_pca?: PcaPoint[];
  sample_correlation?: SampleCorrelation;
  pydeseq2_version?: string;
  tested_by?: string;
};

/**
 * What a differential expression run produced: how the samples cluster, the
 * shape of the result, and the full sortable gene table.
 *
 * Ordered deliberately. The sample projection comes first because it is the
 * plot that can invalidate everything below it -- a replicate sitting with the
 * wrong group means the contrast tested a design that does not match the
 * samples, and no amount of reading the p-value table will reveal that.
 */
export function ExpressionResults({ obj }: { obj: ObjectDetailData }) {
  const f = obj.facts as DeFacts;

  const [sort, setSort] = useState<string>("padj");
  const [direction, setDirection] = useState<"asc" | "desc">("asc");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(0);
  const [onlySignificant, setOnlySignificant] = useState(false);
  const [targetNode, setTargetNode] = useState("");

  const alpha = f.alpha ?? 0.05;

  const { data: plotData } = useQuery({
    queryKey: ["de", "plot", obj.id],
    queryFn: () =>
      api.deResults(obj.id, {
        offset: 0,
        limit: PLOT_LIMIT,
        sort: "padj",
        direction: "asc",
      }),
  });

  const { data: page_, isFetching } = useQuery({
    queryKey: [
      "de",
      "table",
      obj.id,
      sort,
      direction,
      search,
      page,
      onlySignificant,
    ],
    queryFn: () =>
      api.deResults(obj.id, {
        offset: page * PAGE_SIZE,
        limit: PAGE_SIZE,
        sort,
        direction,
        search: search.trim() || undefined,
        max_padj: onlySignificant ? alpha : undefined,
      }),
    // The table should not blank between pages -- a flash of empty rows reads
    // as "no results" rather than "loading the next fifty".
    placeholderData: keepPreviousData,
  });

  const rows = page_?.rows ?? [];
  const total = page_?.total ?? 0;
  const plotRows: DeRow[] = plotData?.rows ?? [];

  const toggleSort = (column: string) => {
    if (sort === column) {
      setDirection((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSort(column);
      // Sensible first direction per column: most significant first for the
      // p-value columns, largest first for magnitudes. Landing on the least
      // interesting end of a column you just clicked is a small papercut that
      // happens every single time.
      setDirection(column === "padj" || column === "p_value" ? "asc" : "desc");
    }
    setPage(0);
  };

  const sortIndicator = (column: string) =>
    sort === column ? (direction === "asc" ? " ▲" : " ▼") : "";

  return (
    <>
      <div className="qc-provenance">
        {[
          f.tested_by === "pydeseq2" && f.pydeseq2_version
            ? `PyDESeq2 ${f.pydeseq2_version}`
            : "PyDESeq2",
          f.contrast_test && f.contrast_reference
            ? `${f.contrast_test} vs ${f.contrast_reference}`
            : null,
          f.samples != null
            ? `${f.samples} sample${f.samples === 1 ? "" : "s"}`
            : null,
        ]
          .filter(Boolean)
          .join(" · ")}
      </div>

      <div className="section">
        <div className="section-title">Summary</div>
        <table className="facts-table">
          <tbody>
            <tr>
              <th>
                Contrast
                <InfoMarker metric="ui.de_contrast" />
              </th>
              <td>
                {f.contrast_test} vs {f.contrast_reference}
                {f.samples_by_condition && (
                  <>
                    {" "}
                    (
                    {Object.entries(f.samples_by_condition)
                      .map(([c, n]) => `${c}: ${n}`)
                      .join(", ")}
                    )
                  </>
                )}
              </td>
            </tr>
            <tr>
              <th>
                Genes tested
                <InfoMarker metric="ui.de_genes_tested" />
              </th>
              <td>
                {f.genes_tested?.toLocaleString() ?? "—"}
                {f.genes_in_matrix != null && (
                  <span className="section-note">
                    {" "}
                    of {f.genes_in_matrix.toLocaleString()} counted — the rest
                    were filtered out for having too few reads to test
                  </span>
                )}
              </td>
            </tr>
            <tr>
              <th>
                Significant
                <InfoMarker metric="ui.de_significant" />
              </th>
              <td>
                {f.significant_genes?.toLocaleString() ?? "—"} at padj &lt;{" "}
                {alpha}
                {f.significant_up != null && f.significant_down != null && (
                  <>
                    {" "}
                    ({f.significant_up.toLocaleString()} up,{" "}
                    {f.significant_down.toLocaleString()} down in{" "}
                    {f.contrast_test})
                  </>
                )}
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <NodeSelector value={targetNode} onChange={setTargetNode} />

      <AiSummary
        facts={obj.facts}
        objectId={obj.id}
        fingerprint={obj.summary_fingerprint ?? undefined}
        factPrefix="ai_de_summary"
        statusFn={() => api.deSummaryStatus()}
        launchFn={(id) => api.launchDeSummary(id)}
        emptyLabel="No summary yet for this result."
      />

      {f.sample_pca && f.sample_pca.length > 0 && (
        <div className="section">
          <div className="section-title">
            Sample clustering
            <InfoMarker metric="ui.chart_sample_pca" />
          </div>
          <SamplePcaPlot points={f.sample_pca} />
          <div className="section-note">
            Replicates of a condition should sit together. One sitting with the
            other group usually means a mislabelled sample — worth resolving
            before reading anything below, since the test assumed the labels
            were right.
          </div>
        </div>
      )}

      {f.sample_correlation && f.sample_correlation.samples?.length > 0 && (
        <div className="section">
          <div className="section-title">
            Sample correlation
            <InfoMarker metric="ui.chart_sample_correlation" />
          </div>
          <SampleCorrelationHeatmap data={f.sample_correlation} />
          <div className="section-note">
            How strongly each pair of samples agrees, over the same genes the
            projection above uses. Replicates should form a bright block on the
            diagonal. A block that does not line up with the conditions is
            structure the first two components missed — often a batch effect.
          </div>
        </div>
      )}

      <div className="section">
        <div className="section-title">
          Volcano
          <InfoMarker metric="ui.chart_volcano" />
        </div>
        <VolcanoPlot rows={plotRows} alpha={alpha} />
        <div className="section-note">
          Coloured points clear both padj &lt; {alpha} and a two-fold change.
          Red is up in {f.contrast_test}, blue is down.
        </div>
      </div>

      <div className="section">
        <div className="section-title">
          MA
          <InfoMarker metric="ui.chart_ma" />
        </div>
        <MAPlot rows={plotRows} alpha={alpha} />
        <div className="section-note">
          Fold change against expression level. A funnel widening to the left
          means the largest changes come from genes with very few reads, where
          the ratio is noise.
        </div>
      </div>

      <div className="section">
        <div className="section-title">
          Genes
          <InfoMarker metric="ui.de_gene_table" />
        </div>

        <div className="detail-actions" style={{ marginBottom: 8 }}>
          <input
            type="search"
            placeholder="Find a gene…"
            aria-label="Find a gene"
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setPage(0);
            }}
          />
          <label style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <input
              type="checkbox"
              checked={onlySignificant}
              onChange={(e) => {
                setOnlySignificant(e.target.checked);
                setPage(0);
              }}
            />
            Significant only
          </label>
          <a
            className="btn-text"
            href={api.objectDownloadUrl(obj.id)}
            download
            title="Download the complete results table"
          >
            Download TSV
          </a>
        </div>

        <table className="facts-table">
          <thead>
            <tr>
              {[
                ["gene", "Gene"],
                ["base_mean", "Mean count"],
                ["log2_fold_change", "log₂FC"],
                ["lfc_std_error", "SE"],
                ["padj", "Adjusted p"],
              ].map(([key, label]) => (
                <th
                  key={key}
                  onClick={() => toggleSort(key)}
                  style={{ cursor: "pointer", userSelect: "none" }}
                  title={`Sort by ${label}`}
                >
                  {label}
                  {sortIndicator(key)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.gene}>
                <td>{r.gene}</td>
                <td style={{ textAlign: "right" }}>
                  {r.base_mean != null ? Math.round(r.base_mean).toLocaleString() : "—"}
                </td>
                <td
                  style={{
                    textAlign: "right",
                    color:
                      r.padj != null && r.padj < alpha
                        ? r.log2_fold_change! > 0
                          ? "var(--danger)"
                          : "var(--accent)"
                        : undefined,
                  }}
                >
                  {r.log2_fold_change != null
                    ? r.log2_fold_change.toFixed(2)
                    : "—"}
                </td>
                <td style={{ textAlign: "right" }}>
                  {r.lfc_std_error != null ? r.lfc_std_error.toFixed(2) : "—"}
                </td>
                {/* An em dash, not 1.0: DESeq2 leaves padj unset for genes it
                    filtered out of correction, and showing that as a
                    non-significant p-value would claim it was tested. */}
                <td style={{ textAlign: "right" }} title={r.padj == null ? "Not tested — filtered out for low counts" : undefined}>
                  {r.padj != null ? r.padj.toExponential(2) : "—"}
                </td>
              </tr>
            ))}
            {rows.length === 0 && !isFetching && (
              <tr>
                <td colSpan={5} className="section-note">
                  {search || onlySignificant
                    ? "No genes match."
                    : "No results in this file."}
                </td>
              </tr>
            )}
          </tbody>
        </table>

        <div className="detail-actions" style={{ marginTop: 8 }}>
          <button
            type="button"
            className="btn"
            disabled={page === 0}
            onClick={() => setPage((p) => Math.max(0, p - 1))}
          >
            Previous
          </button>
          <span className="section-note">
            {total === 0
              ? "—"
              : `${page * PAGE_SIZE + 1}–${Math.min((page + 1) * PAGE_SIZE, total)} of ${total.toLocaleString()}`}
          </span>
          <button
            type="button"
            className="btn"
            disabled={(page + 1) * PAGE_SIZE >= total}
            onClick={() => setPage((p) => p + 1)}
          >
            Next
          </button>
        </div>
      </div>
    </>
  );
}
