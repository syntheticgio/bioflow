/**
 * Two reads' mean-quality-per-position curves overlaid on one axis (R5:
 * curve charts overlay on one axis with a legend naming both objects).
 *
 * Modeled on `QualityOverlayChart` (the trimming comparison): same geometry,
 * same fixed 0–42 axis -- Q20/Q30 are absolute quality thresholds, and a
 * chart that rescales makes a bad file look identical to a good one. The
 * difference is the semantics: that chart compares before-vs-after within one
 * read set, this one compares two objects against each other, so the two
 * series are symmetric (neither is the "subject") and both stop at their own
 * read length.
 */
export interface QcSeries {
  name: string;
  /** `{ position, mean }` points from `quality_per_position`. */
  curve: { position: number; mean: number }[];
}

const W = 460;
const H = 210;
const PAD = { top: 10, right: 46, bottom: 40, left: 30 };
const PLOT_W = W - PAD.left - PAD.right;
const PLOT_H = H - PAD.top - PAD.bottom;
const Y_MAX = 42;

/** x maps position across the shared domain; y maps quality 0–42. */
function scale(curves: QcSeries[]) {
  const maxPos = Math.max(
    ...curves.map((c) => c.curve[c.curve.length - 1]?.position ?? 0),
    1,
  );
  const x = (p: number) => PAD.left + ((p - 1) / Math.max(maxPos - 1, 1)) * PLOT_W;
  const y = (q: number) => PAD.top + PLOT_H - (Math.min(q, Y_MAX) / Y_MAX) * PLOT_H;
  return { maxPos, x, y };
}

/** One `<path>` per series. Each line stops where that read set ends. */
function pathFor(curve: { position: number; mean: number }[], x: (p: number) => number, y: (q: number) => number): string {
  return curve.map((p, i) => `${i ? "L" : "M"}${x(p.position)},${y(p.mean)}`).join(" ");
}

const BANDS = [
  { from: 30, to: Y_MAX, color: "var(--success)", label: "good" },
  { from: 20, to: 30, color: "var(--warn)", label: "fair" },
  { from: 0, to: 20, color: "var(--error)", label: "poor" },
];

/** Paired quality curves for two objects. */
export function QualityCompareChart({ a, b }: { a: QcSeries; b: QcSeries }) {
  const curves = [a, b];
  const { maxPos, x, y } = scale(curves);

  const series = [
    { name: a.name, curve: a.curve, color: "#4a9eff", dash: undefined as string | undefined },
    { name: b.name, curve: b.curve, color: "#1565c0", dash: "5 3" as string | undefined },
  ];

  return (
    <div>
      <svg
        width="100%"
        viewBox={`0 0 ${W} ${H}`}
        style={{ maxWidth: W, display: "block" }}
      >
        {BANDS.map((b) => (
          <text
            key={b.label}
            x={W - PAD.right + 5}
            y={(y(b.to) + y(b.from)) / 2 + 3}
            fontSize="9"
            fill={b.color}
            opacity={0.85}
          >
            {b.label}
          </text>
        ))}
        {[10, 20, 30, 40].map((q) => (
          <g key={q}>
            <line
              x1={PAD.left}
              x2={W - PAD.right}
              y1={y(q)}
              y2={y(q)}
              stroke="var(--border)"
              strokeWidth="1"
            />
            <text
              x={PAD.left - 5}
              y={y(q) + 3}
              textAnchor="end"
              fontSize="9"
              fill="var(--text-faint)"
            >
              {q}
            </text>
          </g>
        ))}

        {series.map((s) => (
          <path
            key={s.name}
            d={pathFor(s.curve, x, y)}
            fill="none"
            stroke={s.color}
            strokeWidth="1.8"
            strokeDasharray={s.dash}
          />
        ))}

        <text x={PAD.left} y={H - 20} fontSize="9" fill="var(--text-faint)">1</text>
        <text
          x={W - PAD.right}
          y={H - 20}
          textAnchor="end"
          fontSize="9"
          fill="var(--text-faint)"
        >
          {maxPos}
        </text>
        <text x={W / 2} y={H - 20} textAnchor="middle" fontSize="9" fill="var(--text-faint)">
          position in read (bp)
        </text>

        {/* Legend inside the SVG so an export still says which curve is which. */}
        {series.map((s, i) => (
          <g key={s.name} transform={`translate(${PAD.left + i * 96} ${H - 6})`}>
            <line x1={0} x2={16} y1={-3} y2={-3} stroke={s.color} strokeWidth="1.8" strokeDasharray={s.dash} />
            <text x={20} y={0} fontSize="9" fill="var(--text-faint)">
              {s.name}
            </text>
          </g>
        ))}
      </svg>
    </div>
  );
}
