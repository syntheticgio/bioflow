/**
 * Variant density across the reference, simple bucketed distributions
 * (QUAL, depth), and a mirrored indel length chart. Hand-rolled SVG,
 * matching CoverageChart.tsx -- these are fixed, simple shapes and a
 * charting library would outweigh the rest of the bundle.
 */

import { plotGeometry } from "../lib/chartScaffold";

export function VariantDensityChart({
  bins,
  boundaries,
}: {
  bins: number[];
  boundaries: { contig: string; bin_start: number }[];
}) {
  if (!bins?.length) return null;

  const w = 720;
  const h = 140;
  const { pad, plotW, plotH } = plotGeometry(w, h, {
    top: 10,
    right: 12,
    bottom: 20,
    left: 12,
  });

  const maxVal = Math.max(...bins, 1);
  const barW = plotW / bins.length;
  const x = (i: number) => pad.left + i * barW;

  // Variant density is severely long-tailed (verified on real data: a
  // handful of bins near the max, hundreds in single digits), so scaling
  // bar height linearly against the max renders almost the whole genome as
  // a flat line -- a bin of 2 next to a bin of 205 is under a pixel tall.
  // Square-root the height instead: it compresses the tail enough that
  // sparse bins stay visible while the peak still reads as the peak. Log
  // would flatten real structure too far and needs special-casing for
  // zero-count bins, which sqrt doesn't.
  const scale = (v: number) => Math.sqrt(v) / Math.sqrt(maxVal);
  const barHeight = (v: number) => {
    if (v <= 0) return 0;
    // Any variant present must be visibly different from none -- round up
    // to a 1px floor rather than let a scaled sliver disappear entirely.
    return Math.max(scale(v) * plotH, 1);
  };
  const y = (v: number) => pad.top + plotH - barHeight(v);

  // More than ~40 separators is noise rather than signal at this width, and
  // the first boundary sits at x=0 -- that line is the axis, not a division
  // between contigs.
  const showBoundaries = boundaries.length > 1 && boundaries.length <= 40;
  const firstContig = boundaries[0]?.contig ?? "";

  return (
    <svg
      width="100%"
      viewBox={`0 0 ${w} ${h}`}
      style={{ maxWidth: w, display: "block" }}
      role="img"
      aria-label={`Variant density across ${boundaries.length || 1} contigs, up to ${maxVal.toLocaleString()} variants per bin`}
    >
      {bins.map((count, i) => (
        <rect
          key={i}
          x={x(i)}
          y={y(count)}
          width={Math.max(barW, 1)}
          height={barHeight(count)}
          fill="var(--accent)"
          opacity={0.8}
        >
          <title>
            {count.toLocaleString()} variant{count === 1 ? "" : "s"}
          </title>
        </rect>
      ))}

      {showBoundaries &&
        boundaries.slice(1).map((b) => (
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

      <text x={pad.left} y={h - 4} fontSize="9" fill="var(--text-faint)">
        {firstContig}
      </text>
      <text x={w - pad.right} y={h - 4} fontSize="9" fill="var(--text-faint)" textAnchor="end">
        {maxVal.toLocaleString()}
      </text>
    </svg>
  );
}

export function DistributionChart({
  buckets,
  label,
  format = (v: number) => `${v}`,
}: {
  buckets: { value: number; count: number }[];
  label: string;
  format?: (v: number) => string;
}) {
  if (!buckets?.length) return null;

  const w = 320;
  const h = 120;
  const { pad, plotW, plotH } = plotGeometry(w, h, {
    top: 6,
    right: 6,
    bottom: 18,
    left: 6,
  });

  const maxCount = Math.max(...buckets.map((b) => b.count), 1);
  const barW = plotW / buckets.length;

  return (
    <svg
      width="100%"
      viewBox={`0 0 ${w} ${h}`}
      style={{ maxWidth: w, display: "block" }}
      role="img"
      aria-label={`${label} distribution, ${buckets.length} buckets, up to ${maxCount.toLocaleString()} per bucket`}
    >
      {buckets.map((b, i) => {
        const barH = (b.count / maxCount) * plotH;
        return (
          <rect
            key={i}
            x={pad.left + i * barW}
            y={pad.top + plotH - barH}
            width={Math.max(barW - 1, 1)}
            height={barH}
            fill="var(--accent)"
            opacity={0.8}
          >
            <title>
              {format(b.value)}: {b.count.toLocaleString()}
            </title>
          </rect>
        );
      })}
      <text x={pad.left} y={h - 4} fontSize="9" fill="var(--text-faint)">
        {format(buckets[0].value)}
      </text>
      <text x={w - pad.right} y={h - 4} fontSize="9" fill="var(--text-faint)" textAnchor="end">
        {format(buckets[buckets.length - 1].value)}
      </text>
    </svg>
  );
}

/**
 * Indel lengths as a mirrored bar chart: deletions extend left of the zero
 * line, insertions extend right. The shape is the diagnostic:
 *
 * - A spike at ±1 bp with a steep falloff is homopolymer indel noise, the
 *   characteristic ONT/PacBio-CLR artifact.
 * - Periodicity at multiples of 3 over a coding-dense reference is real
 *   biological signal — in-frame indels surviving selection.
 * - A smooth, symmetric taper is what a healthy short-read callset looks
 *   like.
 *
 * Lengths beyond ±50 bp are truncated on the chart (not dropped from the
 * data) to keep the informative near-zero region from being compressed into
 * a few pixels by a sparse tail of long indels.
 */
export function IndelLengthChart({
  lengths,
}: {
  lengths: { length: number; count: number }[];
}) {
  if (!lengths?.length) return null;

  const w = 320;
  const h = 120;
  const { pad, plotW, plotH } = plotGeometry(w, h, {
    top: 6,
    right: 6,
    bottom: 18,
    left: 6,
  });

  // Separate into deletions (negative) and insertions (positive), sorted by
  // length so bars are drawn in order from the zero line outward.
  const deletions = lengths
    .filter((d) => d.length < 0)
    .sort((a, b) => b.length - a.length);
  const insertions = lengths
    .filter((d) => d.length > 0)
    .sort((a, b) => a.length - b.length);

  // Clamp the visible range to ±50 bp. The zero line sits at the horizontal
  // center; lengths beyond the clamp are drawn at the edge so their count is
  // still visible without compressing the near-zero region.
  const maxLen = Math.min(
    Math.max(
      deletions.length > 0 ? Math.abs(deletions[0].length) : 0,
      insertions.length > 0 ? Math.abs(insertions[insertions.length - 1].length) : 0,
      1,
    ),
    50,
  );

  const centerX = pad.left + plotW / 2;
  const xForLength = (len: number) => {
    const clamped = Math.max(-maxLen, Math.min(maxLen, len));
    return centerX + (clamped / maxLen) * (plotW / 2);
  };

  // Bar width from the minimum gap between consecutive lengths, capped so
  // bars don't get unreasonably wide with sparse data.
  const allSorted = [...lengths].sort((a, b) => a.length - b.length);
  let barW = 6;
  for (let i = 1; i < allSorted.length; i++) {
    const gap = allSorted[i].length - allSorted[i - 1].length;
    if (gap > 0) {
      const gapPx = Math.abs(xForLength(gap) - xForLength(0));
      barW = Math.min(gapPx * 0.8, 8);
      break;
    }
  }

  const maxCount = Math.max(...lengths.map((d) => d.count), 1);
  const barHeight = (count: number) => (count / maxCount) * plotH;
  const y = (count: number) => pad.top + plotH - barHeight(count);

  return (
    <svg
      width="100%"
      viewBox={`0 0 ${w} ${h}`}
      style={{ maxWidth: w, display: "block" }}
      role="img"
      aria-label={`Indel length distribution, ${lengths.length} lengths, up to ${maxCount.toLocaleString()} per length`}
    >
      {/* Zero line */}
      <line
        x1={centerX}
        x2={centerX}
        y1={pad.top}
        y2={pad.top + plotH}
        stroke="var(--border)"
        strokeWidth="1"
      />

      {/* Deletion bars (left of zero) */}
      {deletions.map((d) => {
        const x = xForLength(d.length) - barW;
        const h = barHeight(d.count);
        return (
          <rect
            key={`del-${d.length}`}
            x={x}
            y={y(d.count)}
            width={barW}
            height={h}
            fill="var(--accent)"
            opacity={0.8}
          >
            <title>{`-${Math.abs(d.length)} bp: ${d.count.toLocaleString()}`}</title>
          </rect>
        );
      })}

      {/* Insertion bars (right of zero) */}
      {insertions.map((d) => {
        const x = xForLength(d.length);
        const h = barHeight(d.count);
        return (
          <rect
            key={`ins-${d.length}`}
            x={x}
            y={y(d.count)}
            width={barW}
            height={h}
            fill="var(--accent)"
            opacity={0.8}
          >
            <title>{`+${d.length} bp: ${d.count.toLocaleString()}`}</title>
          </rect>
        );
      })}

      {/* Axis labels */}
      <text x={pad.left} y={h - 4} fontSize="9" fill="var(--text-faint)">
        {`-${maxLen}`}
      </text>
      <text x={centerX} y={h - 4} fontSize="9" fill="var(--text-faint)" textAnchor="middle">
        0
      </text>
      <text x={w - pad.right} y={h - 4} fontSize="9" fill="var(--text-faint)" textAnchor="end">
        {maxLen < 50 ? `+${maxLen}` : `+${maxLen} (truncated)`}
      </text>
    </svg>
  );
}
