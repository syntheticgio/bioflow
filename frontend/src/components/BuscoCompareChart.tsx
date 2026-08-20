/**
 * Two assemblies' BUSCO completeness as paired stacked bars (R5: categorical
 * charts render paired bars). One bar per object, four segments each, sharing
 * a legend that names both objects.
 *
 * A separate component rather than a two-series mode on `BuscoChart` for the
 * same reason `QualityOverlayChart` exists: the single-object chart grades one
 * assembly against the absolute 100% whole; a comparison is about how the two
 * bars line up, and forcing both into `BuscoChart`'s single `pct` vocabulary
 * would couple the comparison to its width/legend bookkeeping.
 */
export interface BuscoSeries {
  name: string;
  singlePct: number;
  duplicatedPct: number;
  fragmentedPct: number;
  missingPct: number;
}

const BAR_W = 150;
const BAR_H = 18;
const GAP = 2;

interface Segment {
  label: string;
  pct: number;
  color: string;
}

/** The four BUSCO categories and their colours, shared by the bars and the
 *  legend so the two can never disagree on what a colour means. */
const SEGMENTS: { label: string; color: string; key: keyof Pick<BuscoSeries, "singlePct" | "duplicatedPct" | "fragmentedPct" | "missingPct"> }[] = [
  { label: "Single-copy", color: "#2e7d32", key: "singlePct" },
  { label: "Duplicated", color: "#f9a825", key: "duplicatedPct" },
  { label: "Fragmented", color: "#ef6c00", key: "fragmentedPct" },
  { label: "Missing", color: "#c62828", key: "missingPct" },
];

function segmentsOf(s: BuscoSeries): Segment[] {
  return SEGMENTS.map((seg) => ({
    label: seg.label,
    pct: s[seg.key],
    color: seg.color,
  })).filter((seg) => seg.pct > 0);
}

/** One stacked bar with a label under it. Exported for direct testing. */
export function BuscoBar({ series, swatch }: { series: BuscoSeries; swatch: string }) {
  const segs = segmentsOf(series);
  if (segs.length === 0) return null;
  const aria =
    `${series.name} BUSCO: ` +
    segs.map((s) => `${s.label} ${s.pct}%`).join(", ");

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <svg width={BAR_W + 1} height={BAR_H} role="img" aria-label={aria}>
        {segs.reduce<{ x: number; els: React.ReactNode[] }>(
          (acc, seg) => {
            const w = Math.max(2, (seg.pct / 100) * BAR_W);
            acc.els.push(
              <rect
                key={seg.label}
                x={acc.x}
                y={0}
                width={w - (acc.x > 0 ? GAP : 0)}
                height={BAR_H}
                fill={seg.color}
                rx={acc.x === 0 ? 3 : 0}
                ry={acc.x === 0 ? 3 : 0}
              />,
            );
            acc.x += w;
            return acc;
          },
          { x: 0, els: [] },
        ).els}
      </svg>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 5,
          fontSize: 11,
          color: "var(--text-faint)",
        }}
      >
        <span
          style={{
            display: "inline-block",
            width: 8,
            height: 8,
            borderRadius: 2,
            backgroundColor: swatch,
          }}
        />
        {series.name}
      </div>
    </div>
  );
}

/** Paired stacked bars for two objects, plus the shared segment legend. */
export function BuscoCompareChart({ a, b }: { a: BuscoSeries; b: BuscoSeries }) {
  return (
    <div style={{ marginTop: 8, marginBottom: 4 }}>
      <div
        style={{
          display: "flex",
          gap: 20,
          flexWrap: "wrap",
        }}
      >
        <BuscoBar series={a} swatch="#4a9eff" />
        <BuscoBar series={b} swatch="#1565c0" />
      </div>
      <div
        style={{
          display: "flex",
          gap: 12,
          flexWrap: "wrap",
          marginTop: 8,
          fontSize: 11,
          color: "var(--text-faint)",
        }}
      >
        {SEGMENTS.map((seg) => (
          <span key={seg.label} style={{ display: "inline-flex", alignItems: "center", gap: 3 }}>
            <span
              style={{
                display: "inline-block",
                width: 8,
                height: 8,
                borderRadius: 2,
                backgroundColor: seg.color,
              }}
            />
            {seg.label}
          </span>
        ))}
      </div>
    </div>
  );
}
