import type { GcBiasBin } from "../api/types";
import { InfoMarker } from "./InfoMarker";

/**
 * Mean read depth per GC-content bin, across the reference.
 *
 * A dome peaking at mid-GC with both tails dropping is PCR amplification
 * bias -- fixable at the bench (a PCR-free prep, a different polymerase),
 * not by re-aligning. A flat line rules that out. A monotonic rise or fall
 * points at a capture/enrichment artifact rather than PCR. None of that is
 * visible in the depth histogram, which shows the distribution's shape but
 * not what it correlates with.
 *
 * Bins with no observed windows are omitted by the backend (gc_coverage.
 * bias_curve), not zero-filled -- so gaps in the line are GC content this
 * reference simply does not contain, not zero-depth regions.
 */
export function GcBiasChart({ curve }: { curve: GcBiasBin[] }) {
  if (!curve?.length) return null;

  const w = 360;
  const h = 160;
  const pad = { top: 10, right: 10, bottom: 26, left: 34 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;

  const points = curve.map((b) => ({
    gc: (b.gc_min + b.gc_max) / 2,
    depth: b.mean_depth,
    bin: b,
  }));

  const maxDepth = Math.max(...points.map((p) => p.depth), 1);
  const x = (gc: number) => pad.left + (gc / 100) * plotW;
  const y = (depth: number) => pad.top + plotH - (depth / maxDepth) * plotH;

  const linePath = points
    .map((p, i) => `${i === 0 ? "M" : "L"}${x(p.gc)},${y(p.depth)}`)
    .join(" ");
  const areaPath =
    `${linePath} L${x(points[points.length - 1].gc)},${pad.top + plotH} ` +
    `L${x(points[0].gc)},${pad.top + plotH} Z`;

  return (
    <div>
      <div style={{ fontSize: 11, color: "var(--text-faint)" }}>
        Coverage vs GC bias
        <InfoMarker metric="ui.chart_gc_bias" />
      </div>
      <svg
        width="100%"
        viewBox={`0 0 ${w} ${h}`}
        style={{ maxWidth: w, display: "block", marginTop: 4 }}
      >
        <path d={areaPath} fill="var(--accent)" opacity={0.15} stroke="none" />
        <path d={linePath} fill="none" stroke="var(--accent)" strokeWidth="1.5" />

        {points.map((p, i) => (
          <circle key={i} cx={x(p.gc)} cy={y(p.depth)} r={2} fill="var(--accent)">
            <title>
              {p.bin.gc_min}-{p.bin.gc_max}% GC: {p.depth.toFixed(1)}× over{" "}
              {p.bin.window_count.toLocaleString()} windows
            </title>
          </circle>
        ))}

        <text x={pad.left} y={h - 4} fontSize="9" fill="var(--text-faint)">
          0% GC
        </text>
        <text
          x={w - pad.right}
          y={h - 4}
          fontSize="9"
          fill="var(--text-faint)"
          textAnchor="end"
        >
          100% GC
        </text>
      </svg>
    </div>
  );
}
