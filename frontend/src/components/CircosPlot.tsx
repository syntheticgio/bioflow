import React, { useMemo } from "react";

// ─── types ────────────────────────────────────────────────────────────

interface ContigData {
  name: string;
  length: number;
  window_bases: number;
  gc: (number | null)[];
  skew: (number | null)[];
  density?: (number | null)[];
  count?: (number | null)[];
}

export interface GcTracksFacts {
  window_count: number;
  contigs: ContigData[];
  gc_tracks_partial?: boolean;
}

export interface DensityTrackFacts {
  window_count: number;
  contigs: ContigData[];
  repeat_density_partial?: boolean;
  gene_density_partial?: boolean;
}

export interface RingDescriptor {
  kind: "gc" | "skew" | "repeat_density" | "gene_density";
  label: string;
}

interface CircosPlotProps {
  tracks: GcTracksFacts;
  rings?: RingDescriptor[];
  title?: string;
}

// ─── constants ────────────────────────────────────────────────────────

const SIZE = 600;
const CENTER = SIZE / 2;
const MAX_RADIUS = 240;
const CONTIG_ARC_HEIGHT = 22;
const RING_GAP = 6;
const RING_HEIGHT = 36;
const ARC_GAP_RAD = 0.012; // gap between contig arcs
const ARC_START = -Math.PI / 2; // top

// ─── theme colours ────────────────────────────────────────────────────

function useColors() {
  return useMemo(() => {
    if (typeof document === "undefined") return Colors.light;
    return document.documentElement.classList.contains("dark")
      ? Colors.dark
      : Colors.light;
  }, []);
}

const Colors = {
  light: {
    bg: "#fafafa",
    arc: "#bdbdbd",
    label: "#555",
    gcLo: "#c8e6c9",
    gcHi: "#2e7d32",
    skewPos: "#42a5f5",
    skewNeg: "#ef5350",
    repeatLo: "#fce4ec",
    repeatHi: "#c62828",
    geneLo: "#e8f5e9",
    geneHi: "#1b5e20",
    baseline: "#ccc",
  },
  dark: {
    bg: "#1e1e1e",
    arc: "#555",
    label: "#ccc",
    gcLo: "#1b5e20",
    gcHi: "#81c784",
    skewPos: "#64b5f6",
    skewNeg: "#e57373",
    repeatLo: "#4a1c1c",
    repeatHi: "#ef5350",
    geneLo: "#1b3a1b",
    geneHi: "#66bb6a",
    baseline: "#444",
  },
};

// ─── SVG helpers ──────────────────────────────────────────────────────

function wedgePath(
  cx: number,
  cy: number,
  outerR: number,
  innerR: number,
  a0: number,
  a1: number
): string {
  const large = a1 - a0 > Math.PI ? 1 : 0;
  const cos0 = Math.cos(a0), sin0 = Math.sin(a0);
  const cos1 = Math.cos(a1), sin1 = Math.sin(a1);
  const xo0 = cx + outerR * cos0, yo0 = cy + outerR * sin0;
  const xo1 = cx + outerR * cos1, yo1 = cy + outerR * sin1;
  const xi1 = cx + innerR * cos1, yi1 = cy + innerR * sin1;
  const xi0 = cx + innerR * cos0, yi0 = cy + innerR * sin0;
  return [
    `M ${xo0} ${yo0}`,
    `A ${outerR} ${outerR} 0 ${large} 1 ${xo1} ${yo1}`,
    `L ${xi1} ${yi1}`,
    `A ${innerR} ${innerR} 0 ${large} 0 ${xi0} ${yi0}`,
    "Z",
  ].join(" ");
}

// ─── component ────────────────────────────────────────────────────────

const CircosPlot: React.FC<CircosPlotProps> = ({
  tracks,
  rings: ringProp,
  title,
}) => {
  const c = useColors();
  const contigs = tracks.contigs || [];

  const rings: RingDescriptor[] = ringProp || [
    { kind: "gc", label: "GC content" },
    { kind: "skew", label: "GC skew" },
  ];

  // ── too many contigs → no render ────────────────────────────────────

  if (contigs.length > 24) return null;

  // ── geometry ────────────────────────────────────────────────────────

  const totalBases = contigs.reduce((s, ctg) => s + ctg.length, 0);
  if (totalBases === 0) return null;

  // Available angle less gaps between contigs
  const gapTotal = contigs.length * ARC_GAP_RAD;
  const availAngle = 2 * Math.PI - Math.min(gapTotal, Math.PI / 8);

  const gcMean = useMemo(() => {
    const vals = contigs.flatMap((ctg) => ctg.gc.filter((v) => v !== null) as number[]);
    return vals.length > 0 ? vals.reduce((s, v) => s + v, 0) / vals.length : 50;
  }, [contigs]);

  // Ring geometry — outermost = contig arcs
  const contigOuterR = MAX_RADIUS;
  const contigInnerR = contigOuterR - CONTIG_ARC_HEIGHT;
  const ringOuterR0 = contigInnerR - RING_GAP;

  // Precompute per-contig angles
  let angle = ARC_START;

  return (
    <div style={{ padding: "8px 0" }}>
      {title && (
        <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 6, color: c.label }}>
          {title}
        </div>
      )}
      {tracks.gc_tracks_partial && (
        <div style={{ fontSize: 10, color: "#999", marginBottom: 4, fontStyle: "italic" }}>
          Showing {contigs.length} largest contigs
        </div>
      )}
      <svg
        viewBox={`0 0 ${SIZE} ${SIZE}`}
        width={SIZE}
        height={SIZE}
        style={{ maxWidth: "100%", background: c.bg, borderRadius: 8 }}
      >
        {/* ── data rings ─────────────────────────────────────────────── */}
        {rings.map((ring, ri) => {
          const rOuter = ringOuterR0 - ri * (RING_HEIGHT + RING_GAP);
          const rInner = rOuter - RING_HEIGHT;
          const rMid = (rOuter + rInner) / 2;

          let angle2 = ARC_START;

          const wedges: React.ReactNode[] = [];

          // Compute max density for normalization (density-based rings only)
          const isDensityRing = ring.kind === "repeat_density" || ring.kind === "gene_density";
          let maxDensity = 0;
          if (isDensityRing) {
            for (const ctg of contigs) {
              if (!ctg.density) continue;
              for (const v of ctg.density) {
                if (v !== null && v > maxDensity) maxDensity = v;
              }
            }
          }

          contigs.forEach((ctg, ci) => {
            const ca = (ctg.length / totalBases) * availAngle;
            let vals: (number | null)[];
            if (ring.kind === "gc") vals = ctg.gc;
            else if (ring.kind === "skew") vals = ctg.skew;
            else vals = ctg.density ?? [];
            const n = vals.length;
            const wa = ca / n;

            for (let wi = 0; wi < n; wi++) {
              const v = vals[wi];
              if (v === null) {
                angle2 += wa;
                continue;
              }
              const a0 = angle2;
              const a1 = a0 + wa * 0.88;
              angle2 += wa;

              let wo: number, wiR: number;
              let fillColor: string;

              if (ring.kind === "gc") {
                // Fill from inner to outer proportional to GC%
                const frac = v / 100;
                wo = rOuter;
                wiR = rOuter - frac * RING_HEIGHT;
                fillColor = v > gcMean ? c.gcHi : c.gcLo;
              } else if (ring.kind === "skew") {
                // Diverging: positive above mid, negative below mid
                if (v > 0) {
                  wo = rMid + (v / 0.5) * (rOuter - rMid) * 0.8;
                  wiR = rMid;
                } else {
                  wo = rMid;
                  wiR = rMid - (Math.abs(v) / 0.5) * (rMid - rInner) * 0.8;
                }
                fillColor = v > 0 ? c.skewPos : c.skewNeg;
              } else if (ring.kind === "repeat_density") {
                // Fill from inner to outer proportional to repeat density
                const frac = maxDensity > 0 ? v / maxDensity : 0;
                wo = rOuter;
                wiR = rOuter - Math.min(frac, 1) * RING_HEIGHT;
                fillColor = frac > 0.5 ? c.repeatHi : c.repeatLo;
              } else {
                // gene_density: fill from inner to outer proportional to gene density
                const frac = maxDensity > 0 ? v / maxDensity : 0;
                wo = rOuter;
                wiR = rOuter - Math.min(frac, 1) * RING_HEIGHT;
                fillColor = frac > 0.5 ? c.geneHi : c.geneLo;
              }

              if (Math.abs(wo - wiR) < 1) continue;

              wedges.push(
                <path
                  key={`${ri}-${ci}-${wi}`}
                  d={wedgePath(CENTER, CENTER, wo, wiR, a0, a1)}
                  fill={fillColor}
                  opacity={ri === 0 ? 0.9 : 0.8}
                  stroke="none"
                />
              );
            }

            // Gap between contigs
            angle2 += ARC_GAP_RAD;
          });

          // Tick baseline for skew ring
          if (ring.kind === "skew") {
            wedges.unshift(
              <circle
                key={`bl-${ri}`}
                cx={CENTER}
                cy={CENTER}
                r={rMid}
                fill="none"
                stroke={c.baseline}
                strokeWidth={0.5}
                opacity={0.5}
              />
            );
          }

          return <g key={ri}>{wedges}</g>;
        })}

        {/* ── contig arcs ────────────────────────────────────────────── */}
        {contigs.map((ctg) => {
          const ca = (ctg.length / totalBases) * availAngle;
          const a0 = angle;
          const a1 = a0 + ca * 0.92;
          const path = wedgePath(CENTER, CENTER, contigOuterR, contigInnerR, a0, a1);

          const midA = a0 + ca / 2;
          const lr = contigOuterR + 18;
          const lx = CENTER + lr * Math.cos(midA);
          const ly = CENTER + lr * Math.sin(midA);
          const labelDeg = ((midA * 180) / Math.PI + 360) % 360;
          const show = ca > 0.08 && ctg.name.length < 12;
          const rot = labelDeg > 90 && labelDeg < 270 ? labelDeg + 180 : labelDeg;

          angle = a0 + ca;

          return (
            <g key={ctg.name}>
              <path d={path} fill={c.arc} stroke="none" opacity={0.6} />
              {show && (
                <text
                  x={lx}
                  y={ly}
                  fill={c.label}
                  fontSize={10}
                  textAnchor="middle"
                  dominantBaseline="middle"
                  transform={rot !== 0 ? `rotate(${rot}, ${lx}, ${ly})` : undefined}
                >
                  {ctg.name}
                </text>
              )}
            </g>
          );
        })}
      </svg>
    </div>
  );
};

export default CircosPlot;
