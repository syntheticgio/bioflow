import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { GcBlobContig } from "../api/types";
import { InfoMarker } from "./InfoMarker";

const W = 480;
const H = 360;
// Point area (not radius) proportional to contig length, per V4/#641: radius
// scaling exaggerates large contigs quadratically, and the whole reason to
// weight by length is to show whether an off-cluster group is a trivial or
// substantial fraction of the assembly -- that comparison only holds if
// area, the visually-integrated quantity, tracks length linearly.
const MIN_RADIUS = 1.5;
const MAX_RADIUS = 14;

/**
 * Per-contig GC vs coverage -- the unlabelled blobplot.
 *
 * Each point is a contig, GC on the x axis, mean depth (log scale) on the
 * y axis, point AREA proportional to contig length. A clean assembly forms
 * one cluster; a contaminant -- a different organism's DNA at a small
 * fraction of total bases but often many contigs -- sits at a different
 * GC/depth coordinate and separates visually, even when every summary
 * statistic looks fine.
 *
 * Unlike ContigDepthChart's 50-bar cap, there is no readability ceiling
 * here -- clustering gets clearer with more points, not noisier. The cap
 * that does apply (cumulative length, V4) exists only so a report with
 * hundreds of thousands of contigs stays a bounded document; it is NOT a
 * "top N by size" readability cap, and dropping it changes what a clean
 * plot means -- which is why the omission line below always renders when
 * anything was dropped.
 *
 * Clusters are NOT taxonomically labelled -- that is a true BlobTools plot
 * and needs classification against a database (#625). Reading a cluster as
 * identified rather than merely separated is the mistake the InfoMarker
 * below exists to head off.
 */
export function ContigBlobChart({ objectId }: { objectId: string }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["gc-blob", objectId],
    queryFn: () => api.gcBlobReport(objectId),
  });

  return (
    <div className="section">
      <div
        className="section-title"
        style={{ display: "flex", alignItems: "center", gap: 8 }}
      >
        <span>GC vs coverage (blobplot)</span>
        <InfoMarker metric="ui.chart_gc_blob" />
      </div>

      {isLoading ? (
        <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
          Loading the per-contig report…
        </div>
      ) : isError || !data || !data.contigs.length ? (
        <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
          Couldn't load the GC-vs-coverage report.
        </div>
      ) : (
        <BlobScatter contigs={data.contigs} droppedCount={data.dropped_count} />
      )}
    </div>
  );
}

function BlobScatter({
  contigs,
  droppedCount,
}: {
  contigs: GcBlobContig[];
  droppedCount: number;
}) {
  const pad = { top: 10, right: 16, bottom: 28, left: 44 };
  const plotW = W - pad.left - pad.right;
  const plotH = H - pad.top - pad.bottom;

  const scored = contigs.filter((c) => c.gc != null);
  const depths = scored.map((c) => Math.max(c.mean_depth, 0.1));
  const minDepth = Math.min(...depths, 0.1);
  const maxDepth = Math.max(...depths, 1);
  const lengths = scored.map((c) => c.length);
  const minLen = Math.min(...lengths, 0);
  const maxLen = Math.max(...lengths, 1);

  const x = (gc: number) => pad.left + (gc / 100) * plotW;
  // Log scale on depth, per the spec -- a linear axis compresses the low-
  // depth contamination cluster against the axis when one organism is much
  // deeper than the other, which is the common case this plot targets.
  const logMin = Math.log10(Math.max(minDepth, 0.1));
  const logMax = Math.log10(Math.max(maxDepth, minDepth * 10));
  const y = (depth: number) => {
    const t = (Math.log10(Math.max(depth, 0.1)) - logMin) / (logMax - logMin || 1);
    return pad.top + plotH - t * plotH;
  };

  // Area, not radius, proportional to length: interpolate the *area*
  // linearly between the min/max radius's implied areas, then take
  // radius = sqrt(area / pi) to get back the radius SVG wants. Interpolating
  // radius directly (radius = MIN_RADIUS + t * (MAX_RADIUS - MIN_RADIUS))
  // would be radius-proportional -- exactly the wrong thing V4 calls out,
  // since the rendered area would then grow with length squared.
  const radiusFor = (length: number) => {
    if (maxLen === minLen) return (MIN_RADIUS + MAX_RADIUS) / 2;
    const t = (length - minLen) / (maxLen - minLen);
    const minArea = Math.PI * MIN_RADIUS ** 2;
    const maxArea = Math.PI * MAX_RADIUS ** 2;
    const area = minArea + t * (maxArea - minArea);
    return Math.sqrt(area / Math.PI);
  };

  return (
    <div style={{ marginTop: 8 }}>
      <svg width="100%" viewBox={`0 0 ${W} ${H}`} style={{ maxWidth: W, display: "block" }}>
        <line
          x1={pad.left} x2={pad.left + plotW} y1={pad.top + plotH} y2={pad.top + plotH}
          stroke="var(--border)"
        />
        <line
          x1={pad.left} x2={pad.left} y1={pad.top} y2={pad.top + plotH}
          stroke="var(--border)"
        />
        {[0, 25, 50, 75, 100].map((tick) => (
          <text key={tick} x={x(tick)} y={pad.top + plotH + 14} fontSize={9} textAnchor="middle" fill="var(--text-faint)">
            {tick}%
          </text>
        ))}
        <text x={pad.left - 6} y={pad.top + 4} fontSize={9} textAnchor="end" fill="var(--text-faint)">
          {maxDepth.toFixed(0)}×
        </text>
        <text x={pad.left - 6} y={pad.top + plotH} fontSize={9} textAnchor="end" fill="var(--text-faint)">
          {minDepth.toFixed(1)}×
        </text>

        {scored.map((c) => (
          <circle
            key={c.contig}
            cx={x(c.gc as number)}
            cy={y(c.mean_depth)}
            r={radiusFor(c.length)}
            fill="var(--accent)"
            opacity={0.55}
          >
            <title>
              {c.contig}: {(c.gc as number).toFixed(1)}% GC, {c.mean_depth.toFixed(1)}× depth, {c.length.toLocaleString()} bp
            </title>
          </circle>
        ))}
      </svg>

      {/* Load-bearing, not decorative: without this line a clean-looking
          plot is indistinguishable from one whose contamination cluster was
          entirely made of short contigs the cap dropped. Rendered whenever
          droppedCount > 0, with no additional gate. */}
      <div style={{ color: "var(--text-faint)", fontSize: 12, marginTop: 4 }}>
        showing {contigs.length.toLocaleString()} contigs covering 99% of bases
        {droppedCount > 0
          ? `; ${droppedCount.toLocaleString()} shorter contig${droppedCount === 1 ? "" : "s"} omitted`
          : ""}
      </div>
    </div>
  );
}
