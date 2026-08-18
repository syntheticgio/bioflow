/**
 * Nx and NGx contiguity curves.
 *
 * Hand-rolled SVG for the same reason the other charts here are: this is a
 * fixed, simple shape, and the smallest charting dependency would outweigh
 * the entire rest of the bundle.
 *
 * The Y axis is logarithmic. On a linear axis every real assembly renders as
 * a cliff pinned against the axis -- contig lengths in an assembly span
 * several orders of magnitude, which is exactly what the reader needs to see.
 */

import { InfoMarker } from "./InfoMarker";

interface Props {
  /** [percent, length] pairs at x = 1..100, from `sequence_nx_curve`. */
  curve: [number, number][];
  /**
   * The assembly's own total length, from `total_bases`.
   *
   * Passed in rather than derived: the curve holds one length per
   * percentile, which is not enough to recover the sum, and NGx needs the
   * real total to scale against expected genome size.
   */
  totalBases: number;
  /** Expected genome size, when known. Enables the NGx curve. */
  genomeSize?: number;
}

const W = 320;
const H = 180;
const PAD_L = 46;
const PAD_R = 10;
const PAD_T = 12;
const PAD_B = 30;
const PLOT_W = W - PAD_L - PAD_R;
const PLOT_H = H - PAD_T - PAD_B;

const NX_COLOR = "#2e7d32";
const NGX_COLOR = "#f9a825";

/** Compact base-count label for axis ticks: 4500000 -> "4.5 Mb". */
function tick(n: number): string {
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)} Gb`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} Mb`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)} kb`;
  return `${n} b`;
}

/**
 * NGx: the same walk as Nx, but against expected genome size instead of the
 * assembly's own total.
 *
 * scale = assemblyTotal / genomeSize. Two cases:
 *
 * - scale < 1 (assembly shorter than the expected genome): the curve
 *   deliberately stops early. The cumulative length never reaches 100% of
 *   that size, and a curve that ends at x=78 is the visualization saying 22%
 *   of the expected genome is not in this file. This is intentional and
 *   unclamped -- extending it to the axis would erase the finding.
 * - scale > 1 (assembly at least as large as the expected genome -- a
 *   plausible reading when genome size is an estimate, or when the assembly
 *   retains duplicated haplotypes): gx = round(x * scale) can exceed 100
 *   while x itself is still under 100. Rather than dropping those points
 *   (which would truncate the curve early and look identical to the
 *   missing-sequence case above, even though nothing is missing), gx is
 *   clamped to 100: the curve reaches the axis, correctly showing the
 *   assembly covers the full expected genome.
 */
function ngxPoints(
  curve: [number, number][],
  assemblyTotal: number,
  genomeSize: number,
): [number, number][] {
  const scale = assemblyTotal / genomeSize;
  const out: [number, number][] = [];
  let lastX = 0;
  for (const [x, length] of curve) {
    const gx = Math.min(100, Math.round(x * scale));
    // Rounding collapses several source points onto one x when the assembly
    // is much shorter than the genome. Keeping only the first of each run
    // preserves a strictly increasing path; without this the line doubles
    // back on itself and renders as a scribble. The same guard, now applied
    // to the clamped value, also stops the loop from adding anything past
    // the first point that reaches x=100.
    if (gx >= 1 && gx > lastX) {
      out.push([gx, length]);
      lastX = gx;
    }
  }
  return out;
}

export function NxChart({ curve, totalBases, genomeSize }: Props) {
  if (!curve || curve.length === 0) return null;

  const lengths = curve.map(([, length]) => length).filter((n) => n > 0);
  if (lengths.length === 0) return null;

  const maxLen = Math.max(...lengths);
  const minLen = Math.min(...lengths);
  // Guard a degenerate log domain: a uniform assembly has max === min.
  const hi = Math.log10(maxLen);
  const lo = Math.log10(Math.max(1, minLen));
  const span = hi - lo || 1;

  const px = (x: number) => PAD_L + (x / 100) * PLOT_W;
  const py = (length: number) =>
    PAD_T + PLOT_H - ((Math.log10(Math.max(1, length)) - lo) / span) * PLOT_H;

  const path = (pts: [number, number][]) =>
    pts.map(([x, l], i) => `${i === 0 ? "M" : "L"}${px(x)},${py(l)}`).join(" ");

  const ngx =
    genomeSize !== undefined && genomeSize > 0 && totalBases > 0
      ? ngxPoints(curve, totalBases, genomeSize)
      : [];

  const aria =
    `Contiguity curve: N50 ${tick(
      curve.find(([x]) => x === 50)?.[1] ?? maxLen,
    )}, longest ${tick(maxLen)}, shortest ${tick(minLen)}` +
    (ngx.length > 0 ? ", with NGx against expected genome size" : "");

  return (
    <div style={{ marginTop: 10 }}>
      <div style={{ fontSize: 11, color: "var(--text-faint)", marginBottom: 4 }}>
        Nx and NGx contiguity <InfoMarker metric="ui.chart_nx" />
      </div>
      <svg width={W} height={H} role="img" aria-label={aria}>
        {/* axes */}
        <line
          x1={PAD_L}
          y1={PAD_T + PLOT_H}
          x2={PAD_L + PLOT_W}
          y2={PAD_T + PLOT_H}
          stroke="var(--border)"
        />
        <line
          x1={PAD_L}
          y1={PAD_T}
          x2={PAD_L}
          y2={PAD_T + PLOT_H}
          stroke="var(--border)"
        />
        {/* Y ticks at the log extremes and midpoint */}
        {[maxLen, Math.round(10 ** ((hi + lo) / 2)), minLen].map((v, i) => (
          <g key={i}>
            <text
              x={PAD_L - 4}
              y={py(v) + 3}
              fontSize={9}
              textAnchor="end"
              fill="var(--text-faint)"
            >
              {tick(v)}
            </text>
          </g>
        ))}
        {/* X ticks */}
        {[0, 25, 50, 75, 100].map((x) => (
          <text
            key={x}
            x={px(x)}
            y={PAD_T + PLOT_H + 12}
            fontSize={9}
            textAnchor="middle"
            fill="var(--text-faint)"
          >
            {x}
          </text>
        ))}
        <text
          x={PAD_L + PLOT_W / 2}
          y={H - 2}
          fontSize={9}
          textAnchor="middle"
          fill="var(--text-faint)"
        >
          % of assembly
        </text>
        {/* curves */}
        <path d={path(curve)} fill="none" stroke={NX_COLOR} strokeWidth={1.75} />
        {ngx.length > 0 && (
          <path
            d={path(ngx)}
            fill="none"
            stroke={NGX_COLOR}
            strokeWidth={1.75}
            strokeDasharray="5 3"
          />
        )}
        {/* legend, only meaningful when there are two lines to tell apart */}
        {ngx.length > 0 && (
          <g>
            <rect x={PAD_L + 6} y={PAD_T + 2} width={9} height={3} fill={NX_COLOR} />
            <text x={PAD_L + 19} y={PAD_T + 6} fontSize={9} fill="var(--text-faint)">
              Nx
            </text>
            <rect x={PAD_L + 44} y={PAD_T + 2} width={9} height={3} fill={NGX_COLOR} />
            <text x={PAD_L + 57} y={PAD_T + 6} fontSize={9} fill="var(--text-faint)">
              NGx
            </text>
          </g>
        )}
      </svg>
    </div>
  );
}
