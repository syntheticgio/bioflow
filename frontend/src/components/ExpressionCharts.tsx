import type { DeRow } from "../api/types";

/**
 * The three plots a differential expression result is read through.
 *
 * Hand-rolled SVG matching VariantCharts.tsx and CoverageChart.tsx -- these
 * are fixed, simple shapes and a charting library would outweigh the rest of
 * the bundle. Same conventions as those files: each returns a bare <svg> and
 * the caller supplies the section heading, axis text uses --text-faint at
 * 9px, and colours come from the theme variables rather than being invented
 * here.
 */

// Up in the test condition, down in it, and everything that did not clear the
// threshold. Three fixed colours rather than a palette: the categories are
// fixed too, and the grey has to read as "background" against both.
const SIG_UP = "var(--danger, #c0392b)";
const SIG_DOWN = "var(--accent, #2471a3)";
const NOT_SIG = "var(--text-faint, #b6bcc4)";

/**
 * Volcano: effect size against significance.
 *
 * The standard reading of a DE result, and the one that shows what a sorted
 * p-value table cannot -- that a gene can be highly significant and barely
 * changed, which is most of what a large, well-powered experiment produces.
 *
 * Genes DESeq2 left untested (padj null) are dropped rather than plotted at
 * zero. They were filtered out for low counts, and a row of them along the
 * bottom axis would read as a mass of measured-but-uninteresting genes, which
 * is the opposite of what happened to them.
 */
export function VolcanoPlot({
  rows,
  alpha = 0.05,
  lfcThreshold = 1,
}: {
  rows: DeRow[];
  alpha?: number;
  lfcThreshold?: number;
}) {
  const points = rows.filter(
    (r) => r.padj != null && r.log2_fold_change != null
  );
  if (!points.length) return null;

  const w = 720;
  const h = 320;
  const pad = { top: 12, right: 16, bottom: 30, left: 44 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;

  // Symmetric x so zero sits in the middle: an asymmetric axis makes a
  // balanced result look one-sided at a glance.
  const maxAbsLfc = Math.max(
    1,
    ...points.map((r) => Math.abs(r.log2_fold_change!))
  );

  // padj underflows to 0 on strong results, and -log10(0) is Infinity. Clamp
  // to the smallest non-zero value actually present so the tallest real point
  // sets the axis instead of an infinity blanking the whole plot.
  const smallestNonZero =
    points.reduce((min, r) => (r.padj! > 0 && r.padj! < min ? r.padj! : min), 1) ||
    1e-300;
  const negLog = (p: number) => -Math.log10(Math.max(p, smallestNonZero));
  const maxY = Math.max(1, ...points.map((r) => negLog(r.padj!)));

  const x = (lfc: number) =>
    pad.left + ((lfc + maxAbsLfc) / (2 * maxAbsLfc)) * plotW;
  const y = (p: number) => pad.top + plotH - (negLog(p) / maxY) * plotH;

  const isSig = (r: DeRow) =>
    r.padj! < alpha && Math.abs(r.log2_fold_change!) >= lfcThreshold;

  return (
    <svg
      width="100%"
      viewBox={`0 0 ${w} ${h}`}
      style={{ maxWidth: w, display: "block" }}
      role="img"
      aria-label={`Volcano plot of ${points.length} genes, ${points.filter(isSig).length} passing the significance and fold-change thresholds`}
    >
      {/* Threshold guides, under the points. */}
      <line
        x1={pad.left}
        x2={w - pad.right}
        y1={y(alpha)}
        y2={y(alpha)}
        stroke="var(--text-faint)"
        strokeDasharray="4 3"
        strokeWidth={1}
        opacity={0.6}
      />
      {[-lfcThreshold, lfcThreshold].map((t) => (
        <line
          key={t}
          x1={x(t)}
          x2={x(t)}
          y1={pad.top}
          y2={pad.top + plotH}
          stroke="var(--text-faint)"
          strokeDasharray="4 3"
          strokeWidth={1}
          opacity={0.6}
        />
      ))}

      {/* Non-significant first, so the handful of significant genes draw on
          top of the cloud rather than under it. */}
      {points
        .filter((r) => !isSig(r))
        .map((r, i) => (
          <circle
            key={`n${i}`}
            cx={x(r.log2_fold_change!)}
            cy={y(r.padj!)}
            r={1.6}
            fill={NOT_SIG}
            opacity={0.5}
          />
        ))}
      {points
        .filter(isSig)
        .map((r, i) => (
          <circle
            key={`s${i}`}
            cx={x(r.log2_fold_change!)}
            cy={y(r.padj!)}
            r={2.2}
            fill={r.log2_fold_change! > 0 ? SIG_UP : SIG_DOWN}
            opacity={0.85}
          >
            <title>{`${r.gene} — log2FC ${r.log2_fold_change!.toFixed(2)}, padj ${r.padj!.toExponential(2)}`}</title>
          </circle>
        ))}

      <text x={pad.left} y={h - 4} fontSize="9" fill="var(--text-faint)">
        −{maxAbsLfc.toFixed(1)} log₂FC
      </text>
      <text x={w / 2} y={h - 4} fontSize="9" fill="var(--text-faint)" textAnchor="middle">
        0
      </text>
      <text
        x={w - pad.right}
        y={h - 4}
        fontSize="9"
        fill="var(--text-faint)"
        textAnchor="end"
      >
        +{maxAbsLfc.toFixed(1)}
      </text>
      <text
        x={4}
        y={pad.top + plotH / 2}
        fontSize="9"
        fill="var(--text-faint)"
        textAnchor="middle"
        transform={`rotate(-90 4 ${pad.top + plotH / 2})`}
      >
        −log₁₀ adjusted p
      </text>
    </svg>
  );
}

/**
 * MA: effect size against expression level.
 *
 * The companion to the volcano, and the one that exposes a specific artefact
 * the volcano hides: fold changes blowing up at low counts. A funnel widening
 * to the left means the biggest changes are coming from genes with almost no
 * reads, where the ratio is noise rather than biology.
 */
export function MAPlot({ rows, alpha = 0.05 }: { rows: DeRow[]; alpha?: number }) {
  const points = rows.filter(
    (r) => r.base_mean != null && r.base_mean > 0 && r.log2_fold_change != null
  );
  if (!points.length) return null;

  const w = 720;
  const h = 260;
  const pad = { top: 12, right: 16, bottom: 30, left: 44 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;

  // log10 on x: base mean spans several orders of magnitude, and a linear
  // axis puts almost every gene in the leftmost few pixels.
  const logMeans = points.map((r) => Math.log10(r.base_mean!));
  const minX = Math.min(...logMeans);
  const maxX = Math.max(...logMeans);
  const spanX = maxX - minX || 1;
  const maxAbsLfc = Math.max(
    1,
    ...points.map((r) => Math.abs(r.log2_fold_change!))
  );

  const x = (v: number) => pad.left + ((Math.log10(v) - minX) / spanX) * plotW;
  const y = (lfc: number) => pad.top + plotH / 2 - (lfc / maxAbsLfc) * (plotH / 2);

  return (
    <svg
      width="100%"
      viewBox={`0 0 ${w} ${h}`}
      style={{ maxWidth: w, display: "block" }}
      role="img"
      aria-label={`MA plot of ${points.length} genes`}
    >
      <line
        x1={pad.left}
        x2={w - pad.right}
        y1={y(0)}
        y2={y(0)}
        stroke="var(--text-faint)"
        strokeWidth={1}
        opacity={0.6}
      />
      {points
        .filter((r) => !(r.padj != null && r.padj < alpha))
        .map((r, i) => (
          <circle
            key={`n${i}`}
            cx={x(r.base_mean!)}
            cy={y(r.log2_fold_change!)}
            r={1.5}
            fill={NOT_SIG}
            opacity={0.45}
          />
        ))}
      {points
        .filter((r) => r.padj != null && r.padj < alpha)
        .map((r, i) => (
          <circle
            key={`s${i}`}
            cx={x(r.base_mean!)}
            cy={y(r.log2_fold_change!)}
            r={2}
            fill={r.log2_fold_change! > 0 ? SIG_UP : SIG_DOWN}
            opacity={0.85}
          >
            <title>{`${r.gene} — mean ${Math.round(r.base_mean!)}, log2FC ${r.log2_fold_change!.toFixed(2)}`}</title>
          </circle>
        ))}

      <text x={pad.left} y={h - 4} fontSize="9" fill="var(--text-faint)">
        mean count (log₁₀)
      </text>
      <text
        x={w - pad.right}
        y={h - 4}
        fontSize="9"
        fill="var(--text-faint)"
        textAnchor="end"
      >
        ±{maxAbsLfc.toFixed(1)} log₂FC
      </text>
    </svg>
  );
}

export type PcaPoint = {
  sample: string;
  condition: string;
  pc1: number;
  pc2: number;
  pc1_pct: number;
  pc2_pct: number;
};

/**
 * Samples projected onto their first two principal components.
 *
 * The most valuable plot here and the one to look at before either of the
 * others. If replicates of a condition do not sit together -- or worse, if one
 * sits squarely inside the other group -- then a label is wrong somewhere, and
 * every p-value in the table is answering a question about a design that does
 * not match the samples.
 */
export function SamplePcaPlot({ points }: { points: PcaPoint[] }) {
  if (!points?.length) return null;

  const w = 460;
  const h = 320;
  const pad = { top: 14, right: 60, bottom: 26, left: 40 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;

  const xs = points.map((p) => p.pc1);
  const ys = points.map((p) => p.pc2);
  // 1.2x margin so a point never renders half outside the frame, and a floor
  // on the span so N identical samples do not divide by zero.
  const spanX = Math.max(Math.max(...xs) - Math.min(...xs), 1e-6) * 1.2;
  const spanY = Math.max(Math.max(...ys) - Math.min(...ys), 1e-6) * 1.2;
  const midX = (Math.max(...xs) + Math.min(...xs)) / 2;
  const midY = (Math.max(...ys) + Math.min(...ys)) / 2;

  const x = (v: number) => pad.left + ((v - midX) / spanX + 0.5) * plotW;
  const y = (v: number) => pad.top + plotH - ((v - midY) / spanY + 0.5) * plotH;

  const conditions = [...new Set(points.map((p) => p.condition))].sort();
  const palette = [SIG_DOWN, SIG_UP, "#28a745", "#8e44ad", "#e67e22"];
  const colorOf = (c: string) => palette[conditions.indexOf(c) % palette.length];

  return (
    <svg
      width="100%"
      viewBox={`0 0 ${w} ${h}`}
      style={{ maxWidth: w, display: "block" }}
      role="img"
      aria-label={`Principal component plot of ${points.length} samples across ${conditions.length} conditions`}
    >
      {points.map((p) => (
        <g key={p.sample}>
          <circle cx={x(p.pc1)} cy={y(p.pc2)} r={5} fill={colorOf(p.condition)}>
            <title>{`${p.sample} (${p.condition})`}</title>
          </circle>
          <text
            x={x(p.pc1) + 8}
            y={y(p.pc2) + 3}
            fontSize="9"
            fill="var(--text-faint)"
          >
            {p.sample}
          </text>
        </g>
      ))}

      {/* Legend inside the SVG rather than as sibling markup, so the whole
          figure stays one scalable unit. */}
      {conditions.map((c, i) => (
        <g key={c} transform={`translate(${w - pad.right + 8} ${pad.top + i * 14})`}>
          <circle cx={0} cy={-3} r={4} fill={colorOf(c)} />
          <text x={9} y={0} fontSize="9" fill="var(--text-faint)">
            {c}
          </text>
        </g>
      ))}

      <text x={pad.left} y={h - 4} fontSize="9" fill="var(--text-faint)">
        PC1 ({points[0].pc1_pct}%)
      </text>
      <text
        x={4}
        y={pad.top + plotH / 2}
        fontSize="9"
        fill="var(--text-faint)"
        textAnchor="middle"
        transform={`rotate(-90 4 ${pad.top + plotH / 2})`}
      >
        PC2 ({points[0].pc2_pct}%)
      </text>
    </svg>
  );
}
