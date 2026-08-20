import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import type { CoverageWindow } from "../api/types";
import { InfoMarker } from "./InfoMarker";
import { Stat } from "./Stat";
import { baselineDepth, depthRatio, formatRatio, shadeFor } from "../lib/depthShade";

/** Geometry for the per-contig depth track. Wide and short: this is read as a
 *  profile along the contig, not as a bar chart to compare heights across. */
const TRACK_W = 720;
const TRACK_H = 88;

/**
 * mosdepth's depth report: how deep coverage runs across the reference, and
 * where it falls off.
 *
 * Deliberately distinct from the `bam_stats` panels above it, which answer
 * the same question at two other resolutions. `SummaryRow` gives one mean for
 * the whole run, and `ContigDepthStrip` gives one mean per contig; neither
 * can show a dropout *inside* a chromosome, which is the case this exists
 * for. The windows here are the same tiling `gc_tracks` uses, so this track
 * lines up with the GC track on the reference's own Results tab.
 *
 * Two modes share this component because mosdepth emits the same rows for
 * both: uniform windows the app generated, or the intervals of a target BED
 * the user uploaded. The report says which, and the copy follows it -- a
 * panel run's rows are named targets, and calling them "windows" would
 * misdescribe what was measured.
 */
export function CoverageDepth({ objectId }: { objectId: string }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["coverage", objectId],
    queryFn: () => api.coverageReport(objectId),
  });

  const contigNames = data ? Object.keys(data.regions) : [];
  const [selected, setSelected] = useState<string | null>(null);
  const active = selected ?? contigNames[0] ?? null;

  return (
    <div className="section">
      <div
        className="section-title"
        style={{ display: "flex", alignItems: "center", gap: 8 }}
      >
        <span>Coverage depth</span>
        <InfoMarker metric="ui.coverage_depth" />
      </div>

      {isLoading ? (
        <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
          Loading the coverage report…
        </div>
      ) : isError || !data || !data.total ? (
        <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
          Couldn't load the coverage report.
        </div>
      ) : (
        <>
          <div style={{ display: "flex", gap: 20, flexWrap: "wrap", fontSize: 12 }}>
            <Stat
              label="Mean depth"
              metric="ui.coverage_mean_depth"
              value={`${data.total.mean.toFixed(1)}×`}
            />
            <Stat
              label="Max depth"
              metric="ui.coverage_max_depth"
              value={`${data.total.max.toLocaleString()}×`}
            />
            <Stat
              label="Reference"
              metric="ui.coverage_reference_length"
              value={`${data.total.length.toLocaleString()} bp`}
            />
            <Stat
              label={data.mode === "regions" ? "Regions" : "Contigs"}
              metric="ui.coverage_contig_count"
              value={
                data.mode === "regions"
                  ? Object.values(data.regions)
                      .reduce((n, rows) => n + rows.length, 0)
                      .toLocaleString()
                  : data.contigs.length.toLocaleString()
              }
            />
          </div>

          <BreadthRow dist={data.dist} />

          {contigNames.length > 1 && (
            <div
              style={{
                display: "flex",
                gap: 6,
                flexWrap: "wrap",
                margin: "10px 0 4px",
              }}
            >
              {contigNames.map((name) => (
                <button
                  key={name}
                  type="button"
                  className={`contig-chip${name === active ? " contig-chip-on" : ""}`}
                  onClick={() => setSelected(name)}
                >
                  {name}
                </button>
              ))}
            </div>
          )}

          {active && (
            <DepthTrack
              contig={active}
              windows={data.regions[active] ?? []}
              mode={data.mode}
            />
          )}
        </>
      )}
    </div>
  );
}

/**
 * Breadth of coverage at the thresholds the backend recorded.
 *
 * Read from `dist` rather than from the summary's mean, because they answer
 * different questions: a mean of 30x is compatible with half the reference at
 * zero, and it is the fraction at >= 1x that says so.
 */
function BreadthRow({ dist }: { dist: Record<string, number> }) {
  const thresholds = [1, 10, 30].filter((t) => dist[String(t)] != null);
  if (!thresholds.length) return null;

  return (
    <div
      style={{
        display: "flex",
        gap: 20,
        flexWrap: "wrap",
        fontSize: 12,
        marginTop: 8,
      }}
    >
      {thresholds.map((t) => (
        <Stat
          key={t}
          label={`Bases ≥ ${t}×`}
          metric={`ui.coverage_pct_at_${t}x`}
          value={`${(dist[String(t)] * 100).toFixed(1)}%`}
        />
      ))}
    </div>
  );
}

/**
 * One contig's depth profile, as an area filled per window.
 *
 * Shaded against the same baseline machinery `ContigDepthStrip` uses, so a
 * window at half the genome's depth reads the same colour here as a whole
 * contig at half depth does there -- the two charts are the same question at
 * different resolutions and should not disagree about what "low" looks like.
 */
function DepthTrack({
  contig,
  windows,
  mode,
}: {
  contig: string;
  windows: CoverageWindow[];
  mode: "windows" | "regions";
}) {
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  if (!windows.length) return null;

  const pad = { top: 8, right: 12, bottom: 18, left: 44 };
  const plotW = TRACK_W - pad.left - pad.right;
  const plotH = TRACK_H - pad.top - pad.bottom;

  const depths = windows.map((w) => w.depth);
  const maxDepth = Math.max(...depths, 1);
  // Same helper the per-contig strip uses; see depthShade for why this is not
  // the summary's own mean.
  const baseline = baselineDepth(
    windows.map((w) => ({ mean_depth: w.depth, length: w.end - w.start })),
  );

  const barW = plotW / windows.length;
  const x = (i: number) => pad.left + i * barW;
  const y = (d: number) => pad.top + plotH - (d / maxDepth) * plotH;

  const hovered = hoverIdx != null ? windows[hoverIdx] : null;

  return (
    <div style={{ marginTop: 8 }}>
      <svg
        width={TRACK_W}
        height={TRACK_H}
        role="img"
        aria-label={`Read depth across ${contig}`}
        onMouseLeave={() => setHoverIdx(null)}
      >
        {/* Baseline, so a dip reads as a dip rather than as the axis. */}
        <line
          x1={pad.left}
          x2={pad.left + plotW}
          y1={y(baseline ?? 0)}
          y2={y(baseline ?? 0)}
          stroke="var(--border)"
          strokeDasharray="3 3"
        />
        {windows.map((w, i) => {
          const ratio = depthRatio(w.depth, baseline);
          const shade = shadeFor(ratio);
          return (
            <g key={`${w.start}-${w.end}`} className={`depth-bar is-${shade.kind}`}>
              <rect
                x={x(i)}
                y={y(w.depth)}
                width={Math.max(barW, 1)}
                height={pad.top + plotH - y(w.depth)}
                // Same split as ContigDepthStrip: intensity carries the
                // magnitude, the class carries the direction, so both colours
                // stay themeable in CSS rather than hardcoded here.
                opacity={
                  shade.kind === "neutral" || shade.kind === "unknown"
                    ? 1
                    : 0.35 + 0.65 * shade.t
                }
                onMouseEnter={() => setHoverIdx(i)}
              />
            </g>
          );
        })}
        <text x={4} y={pad.top + 8} fontSize={10} fill="var(--text-faint)">
          {maxDepth.toFixed(0)}×
        </text>
        <text x={4} y={pad.top + plotH} fontSize={10} fill="var(--text-faint)">
          0×
        </text>
      </svg>

      <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
        {hovered ? (
          <>
            {hovered.name ? `${hovered.name} · ` : ""}
            {contig}:{hovered.start.toLocaleString()}-
            {hovered.end.toLocaleString()} · {hovered.depth.toFixed(1)}×
            {/* formatRatio already reads "1.67x typical depth" -- appending
                "of mean" to it produced "1.67x typical depth of mean", and
                "typical" is the median rather than the mean anyway. */}
            {baseline != null && depthRatio(hovered.depth, baseline) != null && (
              <> · {formatRatio(depthRatio(hovered.depth, baseline)!)}</>
            )}
          </>
        ) : (
          <>
            {windows.length.toLocaleString()}{" "}
            {mode === "regions" ? "target region" : "window"}
            {windows.length === 1 ? "" : "s"} across {contig}
          </>
        )}
      </div>
    </div>
  );
}
