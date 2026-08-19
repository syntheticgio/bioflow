import type { SvLengthBucket } from "../api/types";
import { InfoMarker } from "./InfoMarker";

/**
 * How many structural variants fall in each length bin.
 *
 * The bins are log-scaled because SV sizes span five orders of magnitude --
 * linear bins would put nearly every call in the first bar. The shape is
 * what makes a callset readable at a glance: a nanopore callset is dominated
 * by sub-kb events, and a spike in the 1 Mb+ bin is usually a mapping
 * artifact rather than biology.
 *
 * Breakends are absent by construction -- they join two loci and span
 * neither, so they have no length to bin. `sv_db.length_histogram` already
 * excludes them server-side, so every bucket here counts only sized events.
 */
export function SvLengthChart({ buckets }: { buckets: SvLengthBucket[] }) {
  if (!buckets?.length) return null;

  const w = 360;
  const h = 160;
  const pad = { top: 10, right: 10, bottom: 26, left: 34 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;

  const maxCount = Math.max(...buckets.map((b) => b.count), 1);
  const barW = plotW / buckets.length;
  const x = (i: number) => pad.left + i * barW;
  const y = (count: number) => pad.top + plotH - (count / maxCount) * plotH;

  return (
    <div>
      <div style={{ fontSize: 11, color: "var(--text-faint)" }}>
        Structural variants by length
        <InfoMarker metric="ui.chart_sv_length" />
      </div>
      <svg
        width="100%"
        viewBox={`0 0 ${w} ${h}`}
        style={{ maxWidth: w, display: "block", marginTop: 4 }}
        role="img"
        aria-label="Structural variant lengths"
      >
        {buckets.map((b, i) => (
          <rect
            key={b.label}
            x={x(i) + 1}
            y={y(b.count)}
            width={Math.max(barW - 2, 1)}
            height={pad.top + plotH - y(b.count)}
            fill="var(--accent)"
            opacity={0.8}
          >
            <title>{`${b.label}: ${b.count.toLocaleString()}`}</title>
          </rect>
        ))}

        {buckets.map((b, i) => (
          <text
            key={`label-${b.label}`}
            x={x(i) + barW / 2}
            y={h - 4}
            fontSize="9"
            fill="var(--text-faint)"
            textAnchor="middle"
          >
            {b.label}
          </text>
        ))}

        <text
          x={pad.left - 6}
          y={pad.top + 4}
          fontSize="9"
          fill="var(--text-faint)"
          textAnchor="end"
        >
          {maxCount.toLocaleString()}
        </text>
        <text
          x={pad.left - 6}
          y={pad.top + plotH}
          fontSize="9"
          fill="var(--text-faint)"
          textAnchor="end"
        >
          0
        </text>
      </svg>
    </div>
  );
}
