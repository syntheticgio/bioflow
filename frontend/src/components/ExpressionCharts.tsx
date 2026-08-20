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

/**
 * P-value histogram: the raw p-value distribution across every tested gene,
 * in fixed bins of 0.05.
 *
 * The cheapest check that the model fit at all, and the one that can
 * invalidate the volcano and MA plots below it:
 * - uniform across [0,1] with a spike near 0 — healthy: most genes are null,
 *   and real signal sits at the low end;
 * - hill-shaped, peaking in the middle — the test is conservative or the
 *   variance is overestimated, and the volcano reads empty even when there
 *   is signal;
 * - a spike near 1 or a U-shape — the model is misspecified (a batch effect,
 *   a bad design formula), and the volcano and MA are actively misleading.
 *
 * Raw p-values, never padj: an adjusted histogram has a different expected
 * shape and none of that diagnostic value. The bins come from the run's
 * facts, computed over the full gene set — the plot fetch is truncated and
 * sorted for the scatter plots, and would drop exactly the null genes the
 * uniform baseline is made of. The dashed line is the height a bin would
 * have if the null p-values were perfectly uniform: n over the number of
 * bins.
 */
export function PValueHistogram({
  bins,
  n,
  binWidth = 0.05,
}: {
  bins: number[];
  n: number;
  binWidth?: number;
}) {
  if (!bins.length || n <= 0) return null;

  const w = 720;
  const h = 320;
  const pad = { top: 12, right: 16, bottom: 30, left: 44 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;

  const expected = n / bins.length;
  // Headroom so the tallest bar and the reference line clear the top edge.
  const yMax = Math.max(expected, ...bins) * 1.1;
  const barW = plotW / bins.length;

  const x = (i: number) => pad.left + (i / bins.length) * plotW;
  const y = (count: number) => pad.top + plotH - (count / yMax) * plotH;

  return (
    <svg
      width="100%"
      viewBox={`0 0 ${w} ${h}`}
      style={{ maxWidth: w, display: "block" }}
      role="img"
      aria-label={`Histogram of ${n} raw p-values in ${bins.length} bins of ${binWidth}`}
    >
      {bins.map((count, i) => (
        <rect
          key={i}
          x={x(i) + 0.5}
          y={y(count)}
          width={Math.max(barW - 1, 1)}
          height={Math.max(pad.top + plotH - y(count), 0)}
          fill={SIG_DOWN}
          opacity={0.8}
        >
          <title>{`${(i * binWidth).toFixed(2)}–${((i + 1) * binWidth).toFixed(2)}: ${count} genes`}</title>
        </rect>
      ))}

      {/* Where the bins sit if the null p-values are perfectly uniform —
          "is this flat?" is the question the whole chart is about. */}
      <line
        x1={pad.left}
        x2={w - pad.right}
        y1={y(expected)}
        y2={y(expected)}
        stroke="var(--text-faint)"
        strokeDasharray="4 3"
        strokeWidth={1}
        opacity={0.6}
      />

      {[0, 0.25, 0.5, 0.75, 1].map((t) => (
        <text
          key={t}
          x={pad.left + t * plotW}
          y={h - 4}
          fontSize="9"
          fill="var(--text-faint)"
          textAnchor={t === 0 ? "start" : t === 1 ? "end" : "middle"}
        >
          {t}
        </text>
      ))}
      <text
        x={4}
        y={pad.top + plotH / 2}
        fontSize="9"
        fill="var(--text-faint)"
        textAnchor="middle"
        transform={`rotate(-90 4 ${pad.top + plotH / 2})`}
      >
        genes
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

export type SampleCorrelation = {
  method: string;
  samples: string[];
  conditions: string[];
  matrix: number[][];
};

/**
 * Sample-to-sample correlation, shaded, with samples grouped by condition.
 *
 * The companion to the projection, answering what it cannot. PCA shows
 * relative position, so two replicates can sit adjacent on PC1/PC2 while
 * correlating poorly — the first two components may carry only a modest share
 * of the variance. A batch effect orthogonal to both is invisible in the
 * scatter and obvious as a block here. And where PCA says a sample is an
 * outlier, this says what it is and is not similar to.
 *
 * Samples are reordered by condition rather than left in matrix order, so
 * replicate blocks land on the diagonal. Without that the blocks are still
 * present but scattered across the grid, which is the whole thing the plot
 * exists to show.
 */
export function SampleCorrelationHeatmap({ data }: { data: SampleCorrelation }) {
  const { samples, conditions, matrix, method } = data;
  const n = samples?.length ?? 0;
  if (!n || matrix?.length !== n) return null;

  // Grouped by condition, stable within a group so a sample's position is
  // reproducible across runs rather than dependent on sort implementation.
  const conditionOrder = [...new Set(conditions)].sort();
  const order = samples
    .map((_, i) => i)
    .sort(
      (a, b) =>
        conditionOrder.indexOf(conditions[a]) -
          conditionOrder.indexOf(conditions[b]) || a - b
    );

  const cell = Math.max(14, Math.min(34, Math.round(340 / n)));
  // Label gutters sized to the longest sample name so nothing is clipped --
  // this plot is worthless if the user cannot tell which cell is which pair.
  const longest = Math.max(...samples.map((s) => s.length));
  const labelW = Math.min(140, Math.max(48, longest * 5.4 + 8));
  const pad = { top: 8, right: 92, bottom: labelW, left: labelW };
  const grid = cell * n;
  const w = pad.left + grid + pad.right;
  const h = pad.top + grid + pad.bottom;

  // Scale spans the off-diagonal range rather than a fixed [-1, 1]. Real
  // samples in one experiment correlate somewhere in the 0.9s, and a fixed
  // scale renders that as one flat block of colour with every difference the
  // plot exists to show compressed out of it.
  const off = order.flatMap((i) =>
    order.filter((j) => j !== i).map((j) => matrix[i][j])
  );
  const lo = Math.min(...off);
  const hi = Math.max(...off);
  const span = hi - lo || 1e-6;

  // Diverging through a neutral midpoint: blue at the weak end, red at the
  // strong end, so a poorly agreeing pair reads as different in kind rather
  // than merely paler.
  const colorOf = (v: number) => {
    const t = Math.min(1, Math.max(0, (v - lo) / span));
    const mix = (a: number[], b: number[], f: number) =>
      `rgb(${a.map((c, k) => Math.round(c + (b[k] - c) * f)).join(",")})`;
    const cold = [36, 113, 163];
    const mid = [242, 242, 240];
    const warm = [192, 57, 43];
    return t < 0.5 ? mix(cold, mid, t * 2) : mix(mid, warm, (t - 0.5) * 2);
  };

  const legendX = pad.left + grid + 20;
  const legendH = Math.min(grid, 160);

  return (
    <svg
      width="100%"
      viewBox={`0 0 ${w} ${h}`}
      style={{ maxWidth: w, display: "block" }}
      role="img"
      aria-label={`${method} correlation heatmap of ${n} samples, grouped by condition`}
    >
      <defs>
        <linearGradient id="corrScale" x1="0" y1="1" x2="0" y2="0">
          {[0, 0.25, 0.5, 0.75, 1].map((t) => (
            <stop key={t} offset={`${t * 100}%`} stopColor={colorOf(lo + t * span)} />
          ))}
        </linearGradient>
      </defs>

      {order.map((ri, r) =>
        order.map((ci, c) => (
          <rect
            key={`${ri}-${ci}`}
            x={pad.left + c * cell}
            y={pad.top + r * cell}
            width={cell}
            height={cell}
            fill={ri === ci ? "var(--text-faint, #b6bcc4)" : colorOf(matrix[ri][ci])}
            opacity={ri === ci ? 0.35 : 1}
          >
            <title>
              {`${samples[ri]} vs ${samples[ci]} — ${method} ${matrix[ri][ci].toFixed(3)}`}
            </title>
          </rect>
        ))
      )}

      {/* Row labels left, column labels rotated below, both in matrix order
          so the two axes always name the same sample at the same index. */}
      {order.map((si, k) => (
        <text
          key={`r${si}`}
          x={pad.left - 6}
          y={pad.top + k * cell + cell / 2 + 3}
          fontSize="9"
          fill="var(--text-faint)"
          textAnchor="end"
        >
          {samples[si]}
        </text>
      ))}
      {order.map((si, k) => {
        const cx = pad.left + k * cell + cell / 2;
        const cy = pad.top + grid + 6;
        return (
          <text
            key={`c${si}`}
            x={cx}
            y={cy}
            fontSize="9"
            fill="var(--text-faint)"
            textAnchor="end"
            transform={`rotate(-90 ${cx} ${cy})`}
          >
            {samples[si]}
          </text>
        );
      })}

      {/* Condition rules on the diagonal blocks, so where one group ends and
          the next begins is visible without reading the labels. */}
      {order.map((si, k) =>
        k > 0 && conditions[si] !== conditions[order[k - 1]] ? (
          <g key={`sep${si}`} stroke="var(--text)" strokeWidth={1} opacity={0.5}>
            <line
              x1={pad.left}
              x2={pad.left + grid}
              y1={pad.top + k * cell}
              y2={pad.top + k * cell}
            />
            <line
              x1={pad.left + k * cell}
              x2={pad.left + k * cell}
              y1={pad.top}
              y2={pad.top + grid}
            />
          </g>
        ) : null
      )}

      <rect
        x={legendX}
        y={pad.top}
        width={12}
        height={legendH}
        fill="url(#corrScale)"
      />
      <text x={legendX + 18} y={pad.top + 8} fontSize="9" fill="var(--text-faint)">
        {hi.toFixed(3)}
      </text>
      <text
        x={legendX + 18}
        y={pad.top + legendH}
        fontSize="9"
        fill="var(--text-faint)"
      >
        {lo.toFixed(3)}
      </text>
      {/* The method, on the figure rather than only in the caption: Pearson
          and Spearman give different numbers, and a reader comparing against
          another tool needs to know which these are. */}
      <text
        x={legendX}
        y={pad.top + legendH + 22}
        fontSize="9"
        fill="var(--text-faint)"
      >
        {method === "spearman" ? "Spearman ρ" : method}
      </text>
    </svg>
  );
}
