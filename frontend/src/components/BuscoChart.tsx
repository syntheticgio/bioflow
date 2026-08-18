import { InfoMarker } from "./InfoMarker";

interface Props {
  singlePct: number;
  duplicatedPct: number;
  fragmentedPct: number;
  missingPct: number;
  total?: number;
}

const BAR_W = 260;
const BAR_H = 18;
const GAP = 2;

interface Segment {
  label: string;
  pct: number;
  color: string;
}

/** Stacked horizontal bar for BUSCO completeness categories. */
export function BuscoChart({ singlePct, duplicatedPct, fragmentedPct, missingPct, total }: Props) {
  const segments: Segment[] = [
    { label: "Single-copy", pct: singlePct, color: "#2e7d32" },
    { label: "Duplicated", pct: duplicatedPct, color: "#f9a825" },
    { label: "Fragmented", pct: fragmentedPct, color: "#ef6c00" },
    { label: "Missing", pct: missingPct, color: "#c62828" },
  ].filter((s) => s.pct > 0);

  if (segments.length === 0) return null;

  const aria = `BUSCO completeness: ${segments.map((s) => `${s.label} ${s.pct}%`).join(", ")}`;

  return (
    <div style={{ marginTop: 8, marginBottom: 4 }}>
      <div style={{ fontSize: 11, color: "var(--text-faint)", marginBottom: 4 }}>
        BUSCO completeness <InfoMarker metric="ui.chart_busco" />
      </div>
      <svg width={BAR_W + 1} height={BAR_H} role="img" aria-label={aria}>
        {segments.reduce<{ x: number; els: React.ReactNode[] }>(
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
          gap: 12,
          flexWrap: "wrap",
          marginTop: 4,
          fontSize: 11,
          color: "var(--text-faint)",
        }}
      >
        {segments.map((seg) => (
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
            {seg.label} {seg.pct}%
          </span>
        ))}
        {total !== undefined && (
          <span style={{ marginLeft: "auto" }}>{total.toLocaleString()} markers</span>
        )}
      </div>
    </div>
  );
}
