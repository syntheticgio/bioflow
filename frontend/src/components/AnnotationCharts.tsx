import { useState } from "react";
import type { AnnotationContigStat, AnnotationLengthBin } from "../api/types";

/**
 * Charts for the annotation-file (GFF/GTF) Results view. Hand-rolled SVG,
 * matching BuscoChart.tsx / CoverageChart.tsx / VariantCharts.tsx -- these
 * are fixed, simple shapes and a charting library would outweigh the rest
 * of the bundle.
 */

const MAX_CATEGORY_ROWS = 15;
const MAX_CONTIGS = 40;

/** Sorted, capped horizontal bar list: label + bar + count, one row per
 *  category. Shared by FeatureTypeChart and BiotypeChart -- both are
 *  independent counts (not parts of a 100% whole), so unlike BuscoChart's
 *  single segmented bar, each category gets its own row. */
function CategoryBarList({
  counts,
  ariaLabel,
}: {
  counts: Record<string, number>;
  ariaLabel: string;
}) {
  const entries = Object.entries(counts).filter(([, n]) => n > 0);
  if (entries.length === 0) return null;

  entries.sort((a, b) => b[1] - a[1]);
  const shown = entries.slice(0, MAX_CATEGORY_ROWS);
  const hiddenCount = entries.length - shown.length;
  const maxCount = shown[0][1];

  const w = 720;
  const rowH = 18;
  const gap = 3;
  const labelW = 140;
  const countW = 70;
  const barAreaW = w - labelW - countW;
  const h = shown.length * (rowH + gap);

  return (
    <div>
      <svg
        width="100%"
        viewBox={`0 0 ${w} ${h}`}
        style={{ maxWidth: w, display: "block" }}
        role="img"
        aria-label={ariaLabel}
      >
        {shown.map(([label, count], i) => {
          const y = i * (rowH + gap);
          const barW = Math.max((count / maxCount) * barAreaW, 1);
          return (
            <g key={label}>
              <text
                x={labelW - 6}
                y={y + rowH / 2 + 4}
                textAnchor="end"
                fontSize="11"
                fill="var(--text)"
              >
                {label}
              </text>
              <rect
                x={labelW}
                y={y}
                width={barW}
                height={rowH}
                fill="var(--accent)"
                opacity={0.85}
                rx={2}
                ry={2}
              >
                <title>
                  {label}: {count.toLocaleString()}
                </title>
              </rect>
              <text
                x={labelW + barAreaW + countW - 4}
                y={y + rowH / 2 + 4}
                textAnchor="end"
                fontSize="11"
                fill="var(--text-faint)"
              >
                {count.toLocaleString()}
              </text>
            </g>
          );
        })}
      </svg>
      {hiddenCount > 0 && (
        <div style={{ fontSize: 11, color: "var(--text-faint)", marginTop: 4 }}>
          +{hiddenCount} more
        </div>
      )}
    </div>
  );
}

/** Horizontal bar chart of feature counts by type (gene, exon, CDS, ...),
 *  sorted descending, capped to the top ~15 with a "+N more" note. */
export function FeatureTypeChart({ counts }: { counts: Record<string, number> }) {
  if (!counts || Object.keys(counts).length === 0) return null;
  return (
    <CategoryBarList
      counts={counts}
      ariaLabel={`Feature counts by type, ${Object.keys(counts).length} types`}
    />
  );
}

/** Horizontal bar chart of feature counts by biotype (protein_coding,
 *  lncRNA, pseudogene, ...), same shape as FeatureTypeChart. */
export function BiotypeChart({ counts }: { counts: Record<string, number> }) {
  if (!counts || Object.keys(counts).length === 0) return null;
  return (
    <CategoryBarList
      counts={counts}
      ariaLabel={`Feature counts by biotype, ${Object.keys(counts).length} biotypes`}
    />
  );
}

/** Per-contig bar chart shared by FeatureDensityChart and
 *  AnnotationCoverageChart -- both plot one numeric value per contig,
 *  capped to MAX_CONTIGS to stay legible on a fragmented assembly.
 *  Modeled on CoverageChart.tsx's per-position bar layout, simplified: no
 *  log-scale toggle (feature density/coverage-fraction don't have
 *  coverage-depth's dynamic-range problem), but keeps a lightweight hover
 *  readout since it's cheap to reuse and the contig name doesn't fit as an
 *  axis label at this bar count. */
function PerContigBarChart({
  items,
  valueLabel,
  format,
}: {
  items: { name: string; value: number }[];
  valueLabel: string;
  format: (v: number) => string;
}) {
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  if (items.length === 0) return null;

  const shown = items.slice(0, MAX_CONTIGS);
  const hiddenCount = items.length - shown.length;

  const w = 720;
  const h = 160;
  const pad = { top: 10, right: 12, bottom: 20, left: 40 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;

  const maxVal = Math.max(...shown.map((d) => d.value), 1);
  const barW = plotW / shown.length;
  const x = (i: number) => pad.left + i * barW;
  const y = (v: number) => pad.top + plotH - (v / maxVal) * plotH;

  const hovered = hoverIdx != null ? shown[hoverIdx] : null;

  return (
    <div>
      <svg
        width="100%"
        viewBox={`0 0 ${w} ${h}`}
        style={{ maxWidth: w, display: "block" }}
        onMouseLeave={() => setHoverIdx(null)}
        role="img"
        aria-label={`${valueLabel} across ${shown.length} contigs, up to ${format(maxVal)}`}
      >
        {shown.map((d, i) => (
          <rect
            key={d.name}
            x={x(i)}
            y={y(d.value)}
            width={Math.max(barW - 1, 1)}
            height={pad.top + plotH - y(d.value)}
            fill="var(--accent)"
            opacity={hoverIdx === i ? 1 : 0.75}
            onMouseEnter={() => setHoverIdx(i)}
          />
        ))}

        <line
          x1={pad.left}
          x2={pad.left}
          y1={pad.top}
          y2={pad.top + plotH}
          stroke="var(--border)"
          strokeWidth="1"
        />
        <text x={pad.left - 5} y={pad.top + 4} textAnchor="end" fontSize="9" fill="var(--text-faint)">
          {format(maxVal)}
        </text>
        <text x={pad.left - 5} y={pad.top + plotH} textAnchor="end" fontSize="9" fill="var(--text-faint)">
          0
        </text>
      </svg>
      <div style={{ fontSize: 11, color: "var(--text-faint)", marginTop: 2 }}>
        {hovered
          ? `${hovered.name}: ${format(hovered.value)}`
          : hiddenCount > 0
            ? `${shown.length} of ${items.length} contigs shown (+${hiddenCount} more)`
            : `${shown.length} contigs`}
      </div>
    </div>
  );
}

/** Bar chart of features per megabase across contigs. Contigs with an
 *  unknown length (per_mb == null) are filtered out rather than shown as
 *  zero. */
export function FeatureDensityChart({ contigs }: { contigs: AnnotationContigStat[] }) {
  if (!contigs?.length) return null;
  const items = contigs
    .filter((c): c is AnnotationContigStat & { per_mb: number } => c.per_mb != null)
    .map((c) => ({ name: c.name, value: c.per_mb }))
    .sort((a, b) => b.value - a.value);
  if (items.length === 0) return null;

  return (
    <PerContigBarChart
      items={items}
      valueLabel="Features per Mb"
      format={(v) => `${v.toFixed(1)}/Mb`}
    />
  );
}

/** Bar chart of the fraction of each contig covered by at least one
 *  feature, as a percentage. Named AnnotationCoverageChart (not
 *  CoverageChart) to avoid ambiguity with CoverageChart.tsx's
 *  BirdsEyeCoverageChart, a separate BAM/alignment-coverage chart. */
export function AnnotationCoverageChart({ contigs }: { contigs: AnnotationContigStat[] }) {
  if (!contigs?.length) return null;
  const items = contigs
    .filter((c): c is AnnotationContigStat & { covered_fraction: number } => c.covered_fraction != null)
    .map((c) => ({ name: c.name, value: c.covered_fraction * 100 }))
    .sort((a, b) => b.value - a.value);
  if (items.length === 0) return null;

  return (
    <PerContigBarChart
      items={items}
      valueLabel="Feature coverage"
      format={(v) => `${v.toFixed(1)}%`}
    />
  );
}

function formatBp(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n % 1_000_000 === 0 ? 0 : 1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(n % 1_000 === 0 ? 0 : 1)}k`;
  return `${n}`;
}

function binLabel(bin: AnnotationLengthBin): string {
  if (bin.max == null) return `>${formatBp(bin.min)}`;
  if (bin.min <= 0) return `≤${formatBp(bin.max)}`;
  return `${formatBp(bin.min)}-${formatBp(bin.max)}`;
}

/** Bucketed distribution of feature lengths (bp). Modeled directly on
 *  VariantCharts.tsx's VariantDensityChart: feature lengths are typically
 *  long-tailed (many short exons, few long genes), so bar height is
 *  sqrt-scaled against the max bucket count rather than linear -- the same
 *  reasoning documented there for variant density. */
export function LengthHistogram({ bins }: { bins: AnnotationLengthBin[] }) {
  if (!bins?.length) return null;

  const w = 720;
  const h = 140;
  const pad = { top: 10, right: 12, bottom: 24, left: 12 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;

  const maxCount = Math.max(...bins.map((b) => b.count), 1);
  const barW = plotW / bins.length;
  const x = (i: number) => pad.left + i * barW;

  const scale = (v: number) => Math.sqrt(v) / Math.sqrt(maxCount);
  const barHeight = (v: number) => {
    if (v <= 0) return 0;
    return Math.max(scale(v) * plotH, 1);
  };
  const y = (v: number) => pad.top + plotH - barHeight(v);

  return (
    <svg
      width="100%"
      viewBox={`0 0 ${w} ${h}`}
      style={{ maxWidth: w, display: "block" }}
      role="img"
      aria-label={`Feature length distribution, ${bins.length} bins, up to ${maxCount.toLocaleString()} features per bin`}
    >
      {bins.map((bin, i) => (
        <rect
          key={i}
          x={x(i)}
          y={y(bin.count)}
          width={Math.max(barW - 1, 1)}
          height={barHeight(bin.count)}
          fill="var(--accent)"
          opacity={0.8}
        >
          <title>
            {binLabel(bin)} bp: {bin.count.toLocaleString()} feature{bin.count === 1 ? "" : "s"}
          </title>
        </rect>
      ))}

      {bins.map((bin, i) => (
        <text
          key={`label-${i}`}
          x={x(i) + barW / 2}
          y={h - 6}
          textAnchor="middle"
          fontSize="9"
          fill="var(--text-faint)"
        >
          {binLabel(bin)}
        </text>
      ))}
    </svg>
  );
}
