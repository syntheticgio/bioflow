/**
 * Variant density across the reference, and simple bucketed distributions
 * (QUAL, depth). Hand-rolled SVG, matching CoverageChart.tsx -- these are
 * fixed, simple shapes and a charting library would outweigh the rest of
 * the bundle.
 */

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
  const pad = { top: 10, right: 12, bottom: 20, left: 12 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;

  const maxVal = Math.max(...bins, 1);
  const barW = plotW / bins.length;
  const x = (i: number) => pad.left + i * barW;
  const y = (v: number) => pad.top + plotH - (v / maxVal) * plotH;

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
          height={pad.top + plotH - y(count)}
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
  const pad = { top: 6, right: 6, bottom: 18, left: 6 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;

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
