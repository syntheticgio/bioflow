import type { GcBiasBin } from "../api/types";
import { InfoMarker } from "./InfoMarker";

/**
 * Coverage-versus-GC bias curve: normalized mean coverage per GC bin.
 *
 * The shape is the reading.  A dome peaking at mid-GC with both tails dropping
 * is PCR amplification bias; a flat line with a drop only at the extremes is
 * normal; coverage tracking GC upward without limit suggests a library or
 * capture artifact.
 *
 * Normalized to 1.0 at genome-average coverage, with a reference line at y=1.
 * Bins with few windows are visually distinguished from well-supported ones
 * (the deviation from flat is the reading, not the absolute value, and sparse
 * bins at the extremes are noise).
 */
export function GcBiasChart({ bins }: { bins: GcBiasBin[] }) {
  if (!bins?.length) return null;

  const w = 360;
  const h = 160;
  const pad = { top: 10, right: 10, bottom: 26, left: 34 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;

  const maxCoverage = Math.max(
    ...bins.map((b) => b.normalized_coverage),
    1.0,
  );
  // Cap the y-axis at 2.5× so a single outlier doesn't flatten everything
  // else, but never below 2.0× so a flat curve still has room.
  const yMax = Math.max(maxCoverage * 1.1, 2.0);

  // Find the bin with the most windows for opacity scaling.
  const maxWindows = Math.max(...bins.map((b) => b.window_count), 1);

  const barW = plotW / bins.length;
  const x = (i: number) => pad.left + i * barW;
  const y = (v: number) => pad.top + plotH - (v / yMax) * plotH;

  // Only label a few GC bins on the x-axis to avoid crowding.
  const xLabelInterval = Math.max(1, Math.floor(bins.length / 6));

  return (
    <div>
      <div style={{ fontSize: 11, color: "var(--text-faint)" }}>
        Coverage vs GC content
        <InfoMarker metric="ui.chart_gc_bias" />
      </div>
      <svg
        width="100%"
        viewBox={`0 0 ${w} ${h}`}
        style={{ maxWidth: w, display: "block", marginTop: 4 }}
      >
        {/* Reference line at y=1 (genome-average coverage). */}
        <line
          x1={pad.left}
          x2={pad.left + plotW}
          y1={y(1)}
          y2={y(1)}
          stroke="var(--border)"
          strokeWidth="1"
          strokeDasharray="3 2"
        />
        <text
          x={pad.left + plotW}
          y={y(1) - 2}
          textAnchor="end"
          fontSize="9"
          fill="var(--text-faint)"
        >
          1×
        </text>

        {bins.map((b, i) => {
          const barH = Math.max(0, pad.top + plotH - y(b.normalized_coverage));
          const opacity =
            b.window_count > 0
              ? 0.3 + 0.7 * Math.min(b.window_count / maxWindows, 1)
              : 0.15;

          return (
            <rect
              key={i}
              x={x(i)}
              y={y(b.normalized_coverage)}
              width={Math.max(barW - 0.5, 1)}
              height={barH}
              fill="var(--accent)"
              opacity={opacity}
            >
              <title>
                {b.gc_pct}% GC · {b.normalized_coverage.toFixed(2)}×
                {b.window_count > 0
                  ? ` · ${b.window_count.toLocaleString()} windows`
                  : " · no windows"}
              </title>
            </rect>
          );
        })}

        {/* Y-axis labels */}
        <text
          x={pad.left - 4}
          y={pad.top + 8}
          textAnchor="end"
          fontSize="9"
          fill="var(--text-faint)"
        >
          {yMax.toFixed(1)}×
        </text>
        <text
          x={pad.left - 4}
          y={pad.top + plotH}
          textAnchor="end"
          fontSize="9"
          fill="var(--text-faint)"
        >
          0×
        </text>

        {/* X-axis labels */}
        {bins.map((b, i) => {
          if (i % xLabelInterval !== 0 && i !== bins.length - 1) return null;
          return (
            <text
              key={i}
              x={x(i) + barW / 2}
              y={h - 4}
              textAnchor="middle"
              fontSize="9"
              fill="var(--text-faint)"
            >
              {b.gc_pct}%
            </text>
          );
        })}
      </svg>
      <div style={{ fontSize: 10, color: "var(--text-faint)", marginTop: 2 }}>
        GC content (%) →
      </div>
    </div>
  );
}
