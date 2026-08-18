import type { DepthHistogramBucket } from "../api/types";
import { InfoMarker } from "./InfoMarker";

/**
 * How many reference positions sit at each depth.
 *
 * The shape is the point, and it is the one thing the birds-eye chart cannot
 * show: those bins are regional means, and averaging turns a genome that is
 * half 60x and half 0x into something indistinguishable from a flat 30x one.
 * A tight peak here is a uniform library, a long right tail is coverage bias,
 * and two modes flag contamination or a large copy-number change.
 */
export function DepthHistogramChart({
  buckets,
  bucketWidth,
  meanDepth,
}: {
  buckets: DepthHistogramBucket[];
  bucketWidth: number;
  meanDepth?: number;
}) {
  if (!buckets?.length) return null;

  const w = 360;
  const h = 160;
  const pad = { top: 10, right: 10, bottom: 26, left: 34 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;

  const maxCount = Math.max(...buckets.map((b) => b.count), 1);
  const barW = plotW / buckets.length;
  const x = (i: number) => pad.left + i * barW;
  const y = (count: number) => pad.top + plotH - (count / maxCount) * plotH;

  const label = (b: DepthHistogramBucket, i: number) =>
    i === buckets.length - 1
      ? `≥${Math.round(b.depth)}×`
      : `${Math.round(b.depth)}–${Math.round(b.depth + bucketWidth)}×`;

  // Where the mean falls, so a skewed distribution reads as skewed rather
  // than as a peak the viewer has to place against the summary row by eye.
  const meanIdx =
    meanDepth != null && bucketWidth > 0 ? meanDepth / bucketWidth : null;
  const meanX =
    meanIdx != null && meanIdx < buckets.length ? x(meanIdx) : null;

  return (
    <div>
      <div style={{ fontSize: 11, color: "var(--text-faint)" }}>
        Reference positions by depth
        <InfoMarker metric="ui.chart_depth_histogram" />
      </div>
      <svg
        width="100%"
        viewBox={`0 0 ${w} ${h}`}
        style={{ maxWidth: w, display: "block", marginTop: 4 }}
      >
        {buckets.map((b, i) => (
          <rect
            key={i}
            x={x(i)}
            y={y(b.count)}
            width={Math.max(barW - 1, 1)}
            height={pad.top + plotH - y(b.count)}
            fill="var(--accent)"
            opacity={0.8}
          >
            <title>
              {label(b, i)}: {b.count.toLocaleString()} positions
            </title>
          </rect>
        ))}

        {meanX != null && (
          <>
            <line
              x1={meanX}
              x2={meanX}
              y1={pad.top}
              y2={pad.top + plotH}
              stroke="var(--border)"
              strokeWidth="1"
              strokeDasharray="3 2"
            />
            <text
              x={meanX}
              y={pad.top - 2}
              textAnchor="middle"
              fontSize="9"
              fill="var(--text-faint)"
            >
              mean
            </text>
          </>
        )}

        <text x={pad.left} y={h - 4} fontSize="9" fill="var(--text-faint)">
          0×
        </text>
        <text
          x={w - pad.right}
          y={h - 4}
          fontSize="9"
          fill="var(--text-faint)"
          textAnchor="end"
        >
          {label(buckets[buckets.length - 1], buckets.length - 1)}
        </text>
      </svg>
    </div>
  );
}
