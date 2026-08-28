import { useState } from "react";

import { useChartScaffold } from "../lib/chartScaffold";

/**
 * Adapter content and duplication levels.
 *
 * Hand-rolled SVG for the same reason `SequenceCharts.tsx` is: these are
 * fixed, simple shapes, and the smallest charting dependency would outweigh
 * the entire rest of the bundle.
 *
 * Both self-suppress when their facts are absent, so a file QC'd before the
 * contamination scan existed renders the tab exactly as it did before.
 */

interface AdapterSeries {
  name: string;
  values: number[];
}

// Distinct enough to tell six overlapping curves apart, and themeable like
// the base colours in SequenceCharts.
const SERIES_COLORS = [
  "var(--accent)",
  "var(--base-t, #f85149)",
  "var(--base-c, #4a9eff)",
  "var(--base-g, #d29922)",
  "var(--base-a, #3fb950)",
  "var(--base-other, #a371f7)",
  "var(--base-n, #8b949e)",
];

// Wide on the right: the legend sits in that gutter, outside the plot area.
const ADAPTER_PAD = { top: 10, right: 96, bottom: 26, left: 34 };

export function AdapterContentChart({
  positions,
  series,
}: {
  positions: number[];
  series: AdapterSeries[];
}) {
  const {
    width: w,
    height: h,
    pad,
    plotW,
    plotH,
    hover,
    onMouseMove,
    clearHover,
  } = useChartScaffold(460, 210, ADAPTER_PAD, (fraction, { plotFraction }) => {
    // Against the plot area, not the whole SVG: the 96px right margin holding
    // the legend would otherwise shift every hover several positions left.
    const idx = Math.round(plotFraction(fraction) * (positions.length - 1));
    return idx >= 0 && idx < positions.length ? idx : null;
  });
  if (!positions?.length || !series?.length) return null;

  // A probe that never matched is not a finding, and six flat lines along the
  // axis hide the one line that matters. The facts keep every probe; the
  // chart shows the ones with something to say.
  const present = series.filter((s) => s.values.some((v) => v > 0));

  if (!present.length) {
    return (
      <div style={{ color: "var(--text-dim)", fontSize: 12, padding: "8px 0" }}>
        No adapter sequence detected.
      </div>
    );
  }

  // Scaled to what was observed, not to 100%. A 4% adapter curve flattened
  // against a full-height axis communicates nothing, and 4% is worth seeing.
  const observed = Math.max(...present.flatMap((s) => s.values));
  const yMax = Math.max(Math.ceil(observed * 1.15), 1);

  const maxPos = positions[positions.length - 1];
  const x = (p: number) => pad.left + ((p - 1) / Math.max(maxPos - 1, 1)) * plotW;
  const y = (v: number) => pad.top + plotH - (Math.min(v, yMax) / yMax) * plotH;

  const ticks = [0, yMax / 2, yMax];

  return (
    <div>
      <svg
        width="100%"
        viewBox={`0 0 ${w} ${h}`}
        style={{ maxWidth: w, display: "block" }}
        onMouseLeave={clearHover}
        onMouseMove={onMouseMove}
      >
        {ticks.map((t) => (
          <g key={t}>
            <line
              x1={pad.left}
              x2={w - pad.right}
              y1={y(t)}
              y2={y(t)}
              stroke="var(--border)"
              strokeWidth="1"
            />
            <text
              x={pad.left - 5}
              y={y(t) + 3}
              textAnchor="end"
              fontSize="9"
              fill="var(--text-faint)"
            >
              {t.toFixed(t < 10 ? 1 : 0)}%
            </text>
          </g>
        ))}

        {present.map((s, i) => (
          <path
            key={s.name}
            d={s.values
              .map((v, j) => `${j ? "L" : "M"} ${x(positions[j])} ${y(v)}`)
              .join(" ")}
            fill="none"
            stroke={SERIES_COLORS[i % SERIES_COLORS.length]}
            strokeWidth="1.8"
          />
        ))}

        {hover != null && (
          <line
            x1={x(positions[hover])}
            x2={x(positions[hover])}
            y1={pad.top}
            y2={pad.top + plotH}
            stroke="var(--text-faint)"
            strokeWidth="1"
          />
        )}

        {present.map((s, i) => (
          <g key={s.name}>
            <line
              x1={w - pad.right + 6}
              x2={w - pad.right + 16}
              y1={pad.top + 8 + i * 13}
              y2={pad.top + 8 + i * 13}
              stroke={SERIES_COLORS[i % SERIES_COLORS.length]}
              strokeWidth="2"
            />
            <text
              x={w - pad.right + 20}
              y={pad.top + 11 + i * 13}
              fontSize="8"
              fill="var(--text-dim)"
            >
              {s.name}
            </text>
          </g>
        ))}

        <text
          x={pad.left + plotW / 2}
          y={h - 6}
          textAnchor="middle"
          fontSize="9"
          fill="var(--text-faint)"
        >
          position in read (bp)
        </text>
      </svg>

      {hover != null && (
        <div style={{ fontSize: 11, color: "var(--text-dim)" }}>
          Position {positions[hover]}:{" "}
          {present
            .map((s) => `${s.name} ${s.values[hover].toFixed(2)}%`)
            .join(" · ")}
        </div>
      )}
    </div>
  );
}

/**
 * Bars rather than FastQC's line chart: the x axis is ordinal bins of uneven
 * width (1, 2, ... >500, >1k), so a connecting line would imply an
 * interpolation between >500 and >1k that does not exist.
 */
export function DuplicationLevelsChart({
  labels,
  percentages,
  percentUnique,
  scannedReads,
}: {
  labels: string[];
  percentages: number[];
  percentUnique?: number;
  scannedReads?: number;
}) {
  const [hover, setHover] = useState<number | null>(null);
  if (!labels?.length || !percentages?.length) return null;

  const w = 460;
  const h = 210;
  const pad = { top: 10, right: 12, bottom: 34, left: 34 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;

  const yMax = Math.max(Math.ceil(Math.max(...percentages)), 1);
  const barW = plotW / labels.length;
  const y = (v: number) => pad.top + plotH - (Math.min(v, yMax) / yMax) * plotH;

  return (
    <div>
      {percentUnique != null && (
        <div style={{ fontSize: 12, marginBottom: 6 }}>
          <strong>{percentUnique.toFixed(1)}%</strong>
          <span style={{ color: "var(--text-dim)" }}> of the library is unique</span>
        </div>
      )}

      <svg
        width="100%"
        viewBox={`0 0 ${w} ${h}`}
        style={{ maxWidth: w, display: "block" }}
        onMouseLeave={() => setHover(null)}
      >
        {[0, yMax / 2, yMax].map((t) => (
          <g key={t}>
            <line
              x1={pad.left}
              x2={w - pad.right}
              y1={y(t)}
              y2={y(t)}
              stroke="var(--border)"
              strokeWidth="1"
            />
            <text
              x={pad.left - 5}
              y={y(t) + 3}
              textAnchor="end"
              fontSize="9"
              fill="var(--text-faint)"
            >
              {t.toFixed(0)}%
            </text>
          </g>
        ))}

        {percentages.map((p, i) => (
          <rect
            key={labels[i]}
            x={pad.left + i * barW + 1}
            y={y(p)}
            width={Math.max(barW - 2, 1)}
            height={Math.max(pad.top + plotH - y(p), 0)}
            fill="var(--accent)"
            opacity={hover === i ? 1 : 0.75}
            onMouseEnter={() => setHover(i)}
          />
        ))}

        {labels.map((label, i) => (
          <text
            key={label}
            x={pad.left + i * barW + barW / 2}
            y={pad.top + plotH + 12}
            textAnchor="middle"
            fontSize="7"
            fill="var(--text-faint)"
          >
            {label}
          </text>
        ))}

        <text
          x={pad.left + plotW / 2}
          y={h - 4}
          textAnchor="middle"
          fontSize="9"
          fill="var(--text-faint)"
        >
          times a sequence appears
        </text>
      </svg>

      <div style={{ fontSize: 11, color: "var(--text-dim)" }}>
        {hover != null
          ? `Seen ${labels[hover]}x: ${percentages[hover].toFixed(2)}% of the library`
          : scannedReads != null
            ? `${scannedReads.toLocaleString()} reads, whole file`
            : ""}
      </div>
    </div>
  );
}
