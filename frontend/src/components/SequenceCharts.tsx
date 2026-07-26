import { useState } from "react";

/**
 * Base-composition pie and per-position quality curve.
 *
 * Hand-rolled SVG rather than a charting library: these are two fixed,
 * simple shapes, and the smallest chart dependency would outweigh the entire
 * rest of the bundle.
 */

interface BaseCount {
  base: string;
  count: number;
  percent: number;
}

interface QualityPoint {
  position: number;
  mean: number;
  count: number;
}

// Colours follow the convention used by IGV and most genome browsers, so the
// chart reads correctly to anyone who has looked at sequence data before.
const BASE_COLORS: Record<string, string> = {
  A: "#3fb950",
  C: "#4a9eff",
  G: "#d29922",
  T: "#f85149",
  N: "#8b949e",
  Other: "#a371f7",
};

export function BaseCompositionChart({
  composition,
  sampledReads,
  sampledBases,
  gcPercent,
}: {
  composition: BaseCount[];
  sampledReads?: number;
  sampledBases?: number;
  gcPercent?: number;
}) {
  const [hovered, setHovered] = useState<string | null>(null);
  if (!composition?.length) return null;

  const total = composition.reduce((s, c) => s + c.count, 0);
  if (!total) return null;

  const size = 132;
  const r = 56;
  const cx = size / 2;
  const cy = size / 2;

  let angle = -Math.PI / 2; // start at 12 o'clock
  const slices = composition.map((c) => {
    const frac = c.count / total;
    const start = angle;
    const end = angle + frac * Math.PI * 2;
    angle = end;

    // A single-value composition (an all-A file) cannot be drawn as an arc:
    // start and end coincide, so the path collapses. Draw a full circle.
    const full = frac >= 0.9999;
    const large = end - start > Math.PI ? 1 : 0;
    const x1 = cx + r * Math.cos(start);
    const y1 = cy + r * Math.sin(start);
    const x2 = cx + r * Math.cos(end);
    const y2 = cy + r * Math.sin(end);

    return {
      ...c,
      full,
      d: `M ${cx} ${cy} L ${x1} ${y1} A ${r} ${r} 0 ${large} 1 ${x2} ${y2} Z`,
    };
  });

  const active = hovered
    ? composition.find((c) => c.base === hovered)
    : undefined;

  return (
    <div>
      <div style={{ display: "flex", gap: 16, alignItems: "center", flexWrap: "wrap" }}>
        <svg width={size} height={size} style={{ flexShrink: 0 }}>
          {slices.map((s) =>
            s.full ? (
              <circle
                key={s.base}
                cx={cx}
                cy={cy}
                r={r}
                fill={BASE_COLORS[s.base] ?? "#888"}
              />
            ) : (
              <path
                key={s.base}
                d={s.d}
                fill={BASE_COLORS[s.base] ?? "#888"}
                stroke="var(--bg-panel)"
                strokeWidth="1.5"
                opacity={hovered && hovered !== s.base ? 0.35 : 1}
                onMouseEnter={() => setHovered(s.base)}
                onMouseLeave={() => setHovered(null)}
                style={{ cursor: "default", transition: "opacity 0.12s" }}
              />
            ),
          )}
        </svg>

        <div style={{ fontSize: 12, minWidth: 150 }}>
          {composition.map((c) => (
            <div
              key={c.base}
              onMouseEnter={() => setHovered(c.base)}
              onMouseLeave={() => setHovered(null)}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 6,
                marginBottom: 3,
                opacity: hovered && hovered !== c.base ? 0.5 : 1,
              }}
            >
              <span
                style={{
                  width: 10,
                  height: 10,
                  borderRadius: 2,
                  background: BASE_COLORS[c.base] ?? "#888",
                  flexShrink: 0,
                }}
              />
              <span style={{ fontFamily: "var(--mono)", width: 42 }}>{c.base}</span>
              <span style={{ width: 52 }}>{c.percent.toFixed(2)}%</span>
              <span style={{ color: "var(--text-faint)", fontSize: 11 }}>
                {c.count.toLocaleString()}
              </span>
            </div>
          ))}
        </div>
      </div>

      <div style={{ fontSize: 11, color: "var(--text-faint)", marginTop: 8 }}>
        {gcPercent != null && <span>GC {gcPercent.toFixed(2)}% · </span>}
        {active && <span>{active.base}: {active.count.toLocaleString()} · </span>}
        {/* Always state the sample size: the chart otherwise looks like a
            measurement of the whole file. */}
        {sampledReads != null
          ? `sampled ${sampledReads.toLocaleString()} reads`
          : sampledBases != null
            ? `sampled ${sampledBases.toLocaleString()} bases`
            : null}
      </div>
    </div>
  );
}

export function QualityChart({ curve }: { curve: QualityPoint[] }) {
  const [hover, setHover] = useState<QualityPoint | null>(null);
  if (!curve?.length) return null;

  const w = 460;
  const h = 210;
  const pad = { top: 10, right: 46, bottom: 26, left: 30 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;

  const maxPos = curve[curve.length - 1].position;
  // Fixed 0–42 y-axis rather than auto-scaling: Q30 and Q20 are absolute
  // quality thresholds, and rescaling would make a bad file look identical
  // to a good one.
  const yMax = 42;

  const x = (p: number) => pad.left + ((p - 1) / Math.max(maxPos - 1, 1)) * plotW;
  const y = (q: number) => pad.top + plotH - (Math.min(q, yMax) / yMax) * plotH;

  const line = curve.map((p, i) => `${i ? "L" : "M"} ${x(p.position)} ${y(p.mean)}`).join(" ");
  const area =
    `M ${x(curve[0].position)} ${pad.top + plotH} ` +
    curve.map((p) => `L ${x(p.position)} ${y(p.mean)}`).join(" ") +
    ` L ${x(maxPos)} ${pad.top + plotH} Z`;

  const bands = [
    { from: 30, to: yMax, color: "var(--success)", label: "good" },
    { from: 20, to: 30, color: "var(--warn)", label: "fair" },
    { from: 0, to: 20, color: "var(--error)", label: "poor" },
  ];

  return (
    <div>
      <svg
        width="100%"
        viewBox={`0 0 ${w} ${h}`}
        style={{ maxWidth: w, display: "block" }}
        onMouseLeave={() => setHover(null)}
      >
        {/* Q20/Q30 quality bands, the conventional reference lines. */}
        {bands.map((b) => (
          <g key={b.label}>
            <rect
              x={pad.left}
              y={y(b.to)}
              width={plotW}
              height={y(b.from) - y(b.to)}
              fill={b.color}
              opacity={0.1}
            />
            <text
              x={w - pad.right + 5}
              y={(y(b.to) + y(b.from)) / 2 + 3}
              fontSize="9"
              fill={b.color}
              opacity={0.85}
            >
              {b.label}
            </text>
          </g>
        ))}
        {[10, 20, 30, 40].map((q) => (
          <g key={q}>
            <line
              x1={pad.left}
              x2={w - pad.right}
              y1={y(q)}
              y2={y(q)}
              stroke="var(--border)"
              strokeWidth="1"
            />
            <text
              x={pad.left - 5}
              y={y(q) + 3}
              textAnchor="end"
              fontSize="9"
              fill="var(--text-faint)"
            >
              {q}
            </text>
          </g>
        ))}

        <path d={area} fill="var(--accent)" opacity={0.13} />
        <path d={line} fill="none" stroke="var(--accent)" strokeWidth="1.8" />

        {hover && (
          <>
            <line
              x1={x(hover.position)}
              x2={x(hover.position)}
              y1={pad.top}
              y2={pad.top + plotH}
              stroke="var(--text-faint)"
              strokeDasharray="3 3"
            />
            <circle cx={x(hover.position)} cy={y(hover.mean)} r="3.5" fill="var(--accent)" />
          </>
        )}

        {/* Transparent hit area so hovering anywhere reads the nearest point. */}
        <rect
          x={pad.left}
          y={pad.top}
          width={plotW}
          height={plotH}
          fill="transparent"
          onMouseMove={(e) => {
            const box = (e.target as SVGRectElement).getBoundingClientRect();
            const frac = (e.clientX - box.left) / box.width;
            const idx = Math.round(frac * (curve.length - 1));
            setHover(curve[Math.max(0, Math.min(curve.length - 1, idx))]);
          }}
        />

        <text x={pad.left} y={h - 6} fontSize="9" fill="var(--text-faint)">
          1
        </text>
        <text x={w - pad.right} y={h - 6} textAnchor="end" fontSize="9" fill="var(--text-faint)">
          {maxPos}
        </text>
        <text x={w / 2} y={h - 6} textAnchor="middle" fontSize="9" fill="var(--text-faint)">
          position in read (bp)
        </text>
      </svg>

      <div style={{ fontSize: 11, color: "var(--text-faint)", marginTop: 2 }}>
        {hover
          ? `position ${hover.position}: Q${hover.mean.toFixed(1)} (${hover.count.toLocaleString()} reads)`
          : "mean Phred quality per position · hover for detail"}
      </div>
    </div>
  );
}
