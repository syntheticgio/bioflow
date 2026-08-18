import type { ContigCoverage } from "../api/types";
import { InfoMarker } from "./InfoMarker";

const MAX_BARS = 50;

/**
 * Mean depth per contig, against the genome-wide mean.
 *
 * Reads the capped `bam_stats_contigs_top` fact rather than the full report:
 * 50 bars is already the readable limit for this, and a fragmented assembly
 * with thousands of scaffolds would be unreadable at any cap. The complete
 * table stays available as ContigTable and its TSV download.
 *
 * The reference line is what makes this worth plotting rather than reading
 * off the table -- an aneuploidy or a dropped contig reads as a departure
 * from the genome mean, not as an absolute number needing interpretation.
 */
export function ContigDepthChart({
  contigs,
  meanDepth,
  totalContigs,
}: {
  contigs: ContigCoverage[];
  meanDepth?: number;
  totalContigs?: number;
}) {
  if (!contigs?.length) return null;

  const shown = contigs.slice(0, MAX_BARS);
  const rowH = 14;
  const w = 360;
  const labelW = 78;
  const h = shown.length * rowH + 18;

  const maxDepth = Math.max(...shown.map((c) => c.mean_depth), meanDepth ?? 0, 1);
  const barLen = (d: number) => ((w - labelW - 10) * d) / maxDepth;
  const meanX = meanDepth != null ? labelW + barLen(meanDepth) : null;

  const capped = totalContigs != null && totalContigs > shown.length;

  return (
    <div>
      <div style={{ fontSize: 11, color: "var(--text-faint)" }}>
        Mean depth per contig
        {capped ? ` (top ${shown.length} of ${totalContigs.toLocaleString()} by mapped reads)` : ""}
        <InfoMarker metric="ui.chart_contig_depth" />
      </div>
      <svg
        width="100%"
        viewBox={`0 0 ${w} ${h}`}
        style={{ maxWidth: w, display: "block", marginTop: 4 }}
      >
        {shown.map((c, i) => (
          <g key={c.contig}>
            <text
              x={labelW - 4}
              y={i * rowH + 10}
              textAnchor="end"
              fontSize="9"
              fill="var(--text-faint)"
            >
              {c.contig.length > 12 ? `${c.contig.slice(0, 11)}…` : c.contig}
            </text>
            <rect
              x={labelW}
              y={i * rowH + 3}
              width={Math.max(barLen(c.mean_depth), 1)}
              height={rowH - 5}
              fill="var(--accent)"
              opacity={0.8}
            >
              <title>
                {c.contig}: {c.mean_depth.toFixed(1)}× over{" "}
                {c.length.toLocaleString()} bp
              </title>
            </rect>
          </g>
        ))}

        {meanX != null && (
          <line
            x1={meanX}
            x2={meanX}
            y1={0}
            y2={shown.length * rowH}
            stroke="var(--border)"
            strokeWidth="1"
            strokeDasharray="3 2"
          />
        )}
        {meanDepth != null && (
          <text
            x={meanX ?? 0}
            y={h - 4}
            textAnchor="middle"
            fontSize="9"
            fill="var(--text-faint)"
          >
            genome mean {meanDepth.toFixed(1)}×
          </text>
        )}
      </svg>
    </div>
  );
}
