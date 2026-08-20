/**
 * Two alignments' depth histograms overlaid on a shared *absolute depth* axis.
 *
 * The single-object `DepthHistogramChart` places bars by bucket *index*,
 * which is correct there only because `bam_stats_depth_bucket_width` is fixed
 * for that one object. In a comparison the two objects can have different
 * bucket widths (the width derives from mean depth), so index placement would
 * put A's 0–10x bucket beside B's 0–30x bucket -- a category the two do not
 * share. Here x is `bucket.depth`, so the two distributions line up on the
 * depth they actually measure; each bar's width is its own object's bucket
 * width, so a coarse-bucketed object reads as coarse and the shapes stay
 * comparable.
 */
export interface DepthSeries {
  name: string;
  /** `{ depth, count }` buckets from `bam_stats_depth_histogram`. */
  buckets: { depth: number; count: number }[];
  /** That object's bucket width, in depth units. */
  bucketWidth: number;
}

const W = 360;
const H = 160;
const PAD = { top: 10, right: 10, bottom: 26, left: 34 };
const PLOT_W = W - PAD.left - PAD.right;
const PLOT_H = H - PAD.top - PAD.bottom;

/** x places a bucket at its absolute depth; y scales to the max count. */
function scale(series: DepthSeries[]) {
  const maxDepth = Math.max(
    ...series.flatMap((s) => s.buckets.map((b) => b.depth + s.bucketWidth)),
    1,
  );
  const maxCount = Math.max(...series.flatMap((s) => s.buckets.map((b) => b.count)), 1);
  const x = (d: number) => PAD.left + (d / maxDepth) * PLOT_W;
  const y = (count: number) => PAD.top + PLOT_H - (count / maxCount) * PLOT_H;
  return { maxDepth, maxCount, x, y };
}

const COLORS = ["#4a9eff", "#1565c0"];

/** Paired depth histograms on an absolute-depth axis. */
export function DepthCompareChart({ a, b }: { a: DepthSeries; b: DepthSeries }) {
  const series = [a, b];
  const { maxDepth, maxCount, x, y } = scale(series);

  return (
    <div>
      <svg
        width="100%"
        viewBox={`0 0 ${W} ${H}`}
        style={{ maxWidth: W, display: "block" }}
      >
        {/* axes */}
        <line x1={PAD.left} y1={PAD.top + PLOT_H} x2={PAD.left + PLOT_W} y2={PAD.top + PLOT_H} stroke="var(--border)" />
        <line x1={PAD.left} y1={PAD.top} x2={PAD.left} y2={PAD.top + PLOT_H} stroke="var(--border)" />

        {/* Y tick */}
        <text x={PAD.left - 4} y={PAD.top + PLOT_H} textAnchor="end" fontSize="9" fill="var(--text-faint)">
          {maxCount}
        </text>

        {series.map((s, si) =>
          s.buckets.map((bucket) => {
            const bw = Math.max((s.bucketWidth / maxDepth) * PLOT_W, 0.6);
            return (
              <rect
                key={`${s.name}-${bucket.depth}`}
                x={x(bucket.depth)}
                y={y(bucket.count)}
                width={bw}
                height={PAD.top + PLOT_H - y(bucket.count)}
                fill={COLORS[si % COLORS.length]}
                opacity={0.55}
              >
                <title>
                  {s.name} · ~{Math.round(bucket.depth)}×: {bucket.count.toLocaleString()} positions
                </title>
              </rect>
            );
          }),
        )}

        <text x={PAD.left} y={H - 8} fontSize="9" fill="var(--text-faint)">0</text>
        <text
          x={PAD.left + PLOT_W}
          y={H - 8}
          textAnchor="end"
          fontSize="9"
          fill="var(--text-faint)"
        >
          ~{Math.round(maxDepth)}×
        </text>
        <text x={PAD.left + PLOT_W / 2} y={H - 8} textAnchor="middle" fontSize="9" fill="var(--text-faint)">
          depth
        </text>

        {/* Legend naming both objects. */}
        {series.map((s, i) => (
          <g key={s.name} transform={`translate(${PAD.left + i * 96} ${H - 6})`}>
            <rect x={0} y={-5} width={10} height={8} fill={COLORS[i % COLORS.length]} opacity={0.8} />
            <text x={15} y={0} fontSize="9" fill="var(--text-faint)">
              {s.name}
            </text>
          </g>
        ))}
      </svg>
    </div>
  );
}
