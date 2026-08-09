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
// Held as CSS variables with the IGV value as the fallback: a theme can
// retune them to its own inks without touching this file, and any theme that
// says nothing still gets the conventional colours.
const BASE_COLORS: Record<string, string> = {
  A: "var(--base-a, #3fb950)",
  C: "var(--base-c, #4a9eff)",
  G: "var(--base-g, #d29922)",
  T: "var(--base-t, #f85149)",
  N: "var(--base-n, #8b949e)",
  Other: "var(--base-other, #a371f7)",
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
  const r = 46;
  const strokeWidth = 20;
  const cx = size / 2;
  const cy = size / 2;
  const circumference = 2 * Math.PI * r;

  let offset = 0;
  const slices = composition.map((c) => {
    const frac = c.count / total;
    const dash = frac * circumference;
    const s = {
      ...c,
      dash,
      dashoffset: -offset,
    };
    offset += dash;
    return s;
  });

  const active = hovered
    ? composition.find((c) => c.base === hovered)
    : undefined;

  return (
    <div>
      <div style={{ display: "flex", gap: 16, alignItems: "center", flexWrap: "wrap" }}>
        <svg width={size} height={size} style={{ flexShrink: 0 }}>
          <g transform={`rotate(-90 ${cx} ${cy})`}>
            {slices.map((s) => (
              <circle
                key={s.base}
                cx={cx}
                cy={cy}
                r={r}
                fill="none"
                stroke={BASE_COLORS[s.base] ?? "#888"}
                strokeWidth={strokeWidth}
                strokeDasharray={`${s.dash} ${circumference - s.dash}`}
                strokeDashoffset={s.dashoffset}
                opacity={hovered && hovered !== s.base ? 0.35 : 1}
                onMouseEnter={() => setHovered(s.base)}
                onMouseLeave={() => setHovered(null)}
                style={{ cursor: "default", transition: "opacity 0.12s" }}
              />
            ))}
          </g>
          {gcPercent != null && (
            <>
              <text
                x={cx}
                y={cy - 3}
                textAnchor="middle"
                fontSize="18"
                fontWeight="600"
                fill="var(--text)"
              >
                {gcPercent.toFixed(1)}%
              </text>
              <text
                x={cx}
                y={cy + 13}
                textAnchor="middle"
                fontSize="10"
                fill="var(--text-faint)"
              >
                GC
              </text>
            </>
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
        {/* Q20/Q30 quality bands, the conventional reference lines. Labelled
            only -- no background fill, so the curve reads without the extra
            tinting competing with it. */}
        {bands.map((b) => (
          <g key={b.label}>
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

interface GcBin {
  gc_percent: number;
  count: number;
}

interface ExpectedGcCurve {
  percent: number;
  attribution: string;
}

/**
 * Per-sequence GC distribution: how many reads sit at each GC percentage.
 *
 * Distinct from the base-composition pie, which is one whole-file number. A
 * library that is half one organism and half another has an unremarkable
 * aggregate GC and two peaks here -- which is the contamination signal this
 * chart exists to show.
 *
 * The reference curve is drawn only when something can actually say what to
 * expect (a reference genome in the project, or a published figure for a named
 * organism), and it is always labelled with which. When nothing can, the
 * checkbox offers a normal fitted to the observed data instead -- labelled as
 * a fit, because that is all it is. FastQC's equivalent curve is fitted the
 * same way and is routinely misread as an expectation, which is what makes
 * that module famously noisy.
 */
export function GcDistributionChart({
  histogram,
  meanGc,
  expected,
  sampledReads,
}: {
  histogram: GcBin[];
  meanGc?: number;
  expected?: ExpectedGcCurve | null;
  sampledReads?: number;
}) {
  const [showFit, setShowFit] = useState(false);
  const [hover, setHover] = useState<GcBin | null>(null);
  if (!histogram?.length) return null;

  const w = 460;
  const h = 210;
  const pad = { top: 10, right: 16, bottom: 26, left: 38 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;

  const maxCount = Math.max(...histogram.map((b) => b.count));
  if (!maxCount) return null;

  // Fixed 0-100 x-axis rather than fitting to the observed range: GC is a
  // percentage of a fixed scale, and rescaling would make a tight, healthy
  // distribution and a broad, suspicious one look identical.
  const x = (gc: number) => pad.left + (gc / 100) * plotW;
  const y = (count: number) => pad.top + plotH - (count / maxCount) * plotH;

  const line = histogram
    .map((b, i) => `${i ? "L" : "M"} ${x(b.gc_percent)} ${y(b.count)}`)
    .join(" ");

  // A normal fitted to the observed mean and standard deviation, computed
  // from the bins themselves. Scaled to the tallest observed bin so the two
  // curves are comparable in shape, which is the only thing being compared.
  const total = histogram.reduce((s, b) => s + b.count, 0);
  const mean =
    meanGc ?? histogram.reduce((s, b) => s + b.gc_percent * b.count, 0) / total;
  const variance =
    histogram.reduce((s, b) => s + b.count * (b.gc_percent - mean) ** 2, 0) /
    total;
  const sd = Math.sqrt(variance) || 1;
  const fitPath = Array.from({ length: 101 }, (_, gc) => {
    const density = Math.exp(-((gc - mean) ** 2) / (2 * sd * sd));
    return `${gc ? "L" : "M"} ${x(gc)} ${y(density * maxCount)}`;
  }).join(" ");

  return (
    <div>
      <svg
        width="100%"
        viewBox={`0 0 ${w} ${h}`}
        style={{ maxWidth: w, display: "block" }}
        onMouseLeave={() => setHover(null)}
      >
        {[0, 25, 50, 75, 100].map((gc) => (
          <g key={gc}>
            <line
              x1={x(gc)}
              x2={x(gc)}
              y1={pad.top}
              y2={pad.top + plotH}
              stroke="var(--border)"
              strokeWidth="1"
            />
            <text
              x={x(gc)}
              y={h - 14}
              textAnchor="middle"
              fontSize="9"
              fill="var(--text-faint)"
            >
              {gc}
            </text>
          </g>
        ))}

        <path d={line} fill="none" stroke="var(--accent)" strokeWidth="1.8" />

        {/* The expected-GC line, when something can say what to expect. */}
        {expected && (
          <>
            <line
              x1={x(expected.percent)}
              x2={x(expected.percent)}
              y1={pad.top}
              y2={pad.top + plotH}
              stroke="var(--success)"
              strokeWidth="1.5"
              strokeDasharray="4 3"
            />
            <text
              x={x(expected.percent) + 4}
              y={pad.top + 10}
              fontSize="9"
              fill="var(--success)"
            >
              expected
            </text>
          </>
        )}

        {showFit && !expected && (
          <path
            d={fitPath}
            fill="none"
            stroke="var(--text-faint)"
            strokeWidth="1.4"
            strokeDasharray="4 3"
          />
        )}

        {hover && (
          <line
            x1={x(hover.gc_percent)}
            x2={x(hover.gc_percent)}
            y1={pad.top}
            y2={pad.top + plotH}
            stroke="var(--text-faint)"
            strokeDasharray="3 3"
          />
        )}

        <rect
          x={pad.left}
          y={pad.top}
          width={plotW}
          height={plotH}
          fill="transparent"
          onMouseMove={(e) => {
            const box = (e.target as SVGRectElement).getBoundingClientRect();
            const gc = ((e.clientX - box.left) / box.width) * 100;
            let nearest = histogram[0];
            for (const b of histogram) {
              if (Math.abs(b.gc_percent - gc) < Math.abs(nearest.gc_percent - gc)) {
                nearest = b;
              }
            }
            setHover(nearest);
          }}
        />

        <text x={w / 2} y={h - 2} textAnchor="middle" fontSize="9" fill="var(--text-faint)">
          mean GC content per read (%)
        </text>
      </svg>

      <div style={{ fontSize: 11, color: "var(--text-faint)", marginTop: 2 }}>
        {hover
          ? `${hover.gc_percent}% GC: ${hover.count.toLocaleString()} reads`
          : expected
            ? expected.attribution
            : "reads by GC content · hover for detail"}
        {sampledReads != null && ` · sampled ${sampledReads.toLocaleString()} reads`}
      </div>

      {/* Offered only when nothing authoritative can be drawn. With a real
          expected value on the chart, a second dashed curve fitted to the data
          itself would compete with it and say less. */}
      {!expected && (
        <label
          style={{
            fontSize: 11,
            color: "var(--text-faint)",
            display: "flex",
            alignItems: "center",
            gap: 5,
            marginTop: 4,
          }}
        >
          <input
            type="checkbox"
            checked={showFit}
            onChange={(e) => setShowFit(e.target.checked)}
          />
          overlay normal distribution (fitted to this file, not an expectation)
        </label>
      )}
    </div>
  );
}
