import { useState } from "react";
import { InfoMarker } from "./InfoMarker";
import type { CoverageBoundary, CumulativeCoveragePoint } from "../api/types";

/**
 * Coverage across the whole reference, and the cumulative depth curve.
 *
 * Hand-rolled SVG, matching SequenceCharts.tsx: these are fixed, simple
 * shapes and a charting library would outweigh the rest of the bundle.
 *
 * The birds-eye view is deliberately a summary -- ~1000 bins regardless of
 * genome size -- not a genome browser. Anything finer belongs in IGV.
 */

export function BirdsEyeCoverageChart({
  bins,
  boundaries,
}: {
  bins: number[];
  boundaries: CoverageBoundary[];
}) {
  const [logScale, setLogScale] = useState(false);
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  if (!bins?.length) return null;

  const w = 720;
  const h = 160;
  const pad = { top: 10, right: 12, bottom: 20, left: 40 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;

  const scaled = (v: number) => (logScale ? Math.log10(v + 1) : v);
  const maxVal = Math.max(...bins.map(scaled), 1);

  const barW = plotW / bins.length;
  const x = (i: number) => pad.left + i * barW;
  const y = (v: number) => pad.top + plotH - (scaled(v) / maxVal) * plotH;

  const hovered = hoverIdx != null ? bins[hoverIdx] : null;
  const hoveredContig =
    hoverIdx != null
      ? [...boundaries].reverse().find((b) => b.bin_start <= hoverIdx)?.contig
      : null;

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ fontSize: 11, color: "var(--text-faint)" }}>
          Coverage across the reference
          <InfoMarker metric="ui.chart_birds_eye_coverage" />
        </div>
        <label style={{ fontSize: 11, color: "var(--text-faint)", cursor: "pointer" }}>
          <input
            type="checkbox"
            checked={logScale}
            onChange={(e) => setLogScale(e.target.checked)}
            style={{ marginRight: 4 }}
          />
          log scale
        </label>
      </div>

      <svg
        width="100%"
        viewBox={`0 0 ${w} ${h}`}
        style={{ maxWidth: w, display: "block", marginTop: 4 }}
        onMouseLeave={() => setHoverIdx(null)}
      >
        {bins.map((depth, i) => (
          <rect
            key={i}
            x={x(i)}
            y={y(depth)}
            width={Math.max(barW, 1)}
            height={pad.top + plotH - y(depth)}
            fill="var(--accent)"
            opacity={hoverIdx === i ? 1 : 0.75}
            onMouseEnter={() => setHoverIdx(i)}
          />
        ))}

        {/* Contig boundaries as thin separators, so the eye can tell where
            one contig ends and the next begins. */}
        {boundaries.map((b) => (
          <line
            key={b.contig}
            x1={x(b.bin_start)}
            x2={x(b.bin_start)}
            y1={pad.top}
            y2={pad.top + plotH}
            stroke="var(--border)"
            strokeWidth="1"
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
          {Math.round(maxVal >= 1 && logScale ? Math.pow(10, maxVal) - 1 : maxVal)}
        </text>
        <text x={pad.left - 5} y={pad.top + plotH} textAnchor="end" fontSize="9" fill="var(--text-faint)">
          0
        </text>
      </svg>

      <div style={{ fontSize: 11, color: "var(--text-faint)", marginTop: 2 }}>
        {hovered != null
          ? `${hoveredContig ?? "—"}: ${hovered.toFixed(1)}× depth`
          : `${boundaries.length.toLocaleString()} contigs`}
      </div>
    </div>
  );
}

export function CumulativeCoverageChart({ curve }: { curve: CumulativeCoveragePoint[] }) {
  if (!curve?.length) return null;

  const w = 360;
  const h = 160;
  const pad = { top: 10, right: 10, bottom: 26, left: 34 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;

  const maxDepth = Math.max(...curve.map((c) => c.depth), 1);
  const x = (depth: number) => pad.left + (depth / maxDepth) * plotW;
  const y = (fraction: number) => pad.top + plotH - fraction * plotH;

  const sorted = [...curve].sort((a, b) => a.depth - b.depth);
  const line = sorted.map((p, i) => `${i ? "L" : "M"} ${x(p.depth)} ${y(p.fraction)}`).join(" ");

  return (
    <div>
      <div style={{ fontSize: 11, color: "var(--text-faint)" }}>
        Fraction of reference at or above depth
        <InfoMarker metric="ui.chart_cumulative_coverage" />
      </div>
      <svg width="100%" viewBox={`0 0 ${w} ${h}`} style={{ maxWidth: w, display: "block", marginTop: 4 }}>
        {[0, 0.25, 0.5, 0.75, 1].map((f) => (
          <g key={f}>
            <line
              x1={pad.left}
              x2={w - pad.right}
              y1={y(f)}
              y2={y(f)}
              stroke="var(--border)"
              strokeWidth="1"
            />
            <text x={pad.left - 5} y={y(f) + 3} textAnchor="end" fontSize="9" fill="var(--text-faint)">
              {Math.round(f * 100)}%
            </text>
          </g>
        ))}
        <path d={line} fill="none" stroke="var(--accent)" strokeWidth="1.8" />
        {sorted.map((p) => (
          <circle key={p.depth} cx={x(p.depth)} cy={y(p.fraction)} r={2.5} fill="var(--accent)" />
        ))}
        {sorted.map((p) => (
          <text
            key={`label-${p.depth}`}
            x={x(p.depth)}
            y={h - pad.bottom + 12}
            textAnchor="middle"
            fontSize="9"
            fill="var(--text-faint)"
          >
            {p.depth}×
          </text>
        ))}
      </svg>
    </div>
  );
}
