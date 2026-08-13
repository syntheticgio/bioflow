import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { api } from "../api/client";
import type {
  AnnotationContigStat,
  AnnotationWindowFeature,
  ObjectDetail as ObjectDetailData,
} from "../api/types";

/**
 * Features along a coordinate axis: what the summary charts and the feature
 * table cannot show -- clustering, overlap, strand, and exon structure in
 * genomic context.
 *
 * The axis comes from the reference's recorded contig lengths, never from the
 * annotation's own coordinates. A ruler synthesised from the last feature's
 * end would mean "as far as annotation reaches" while looking like the contig
 * length, which is the kind of quietly-wrong picture this view exists to
 * avoid.
 */

const TRACK_WIDTH = 1000;
const ROW_HEIGHT = 18;
const AXIS_HEIGHT = 28;

function fmt(n: number): string {
  return n.toLocaleString();
}

export function AnnotationTrack({
  obj,
  contigs,
  onPickFeature,
}: {
  obj: ObjectDetailData;
  /** Per-contig stats from #257. Only entries with a known length are usable
   *  as an axis; the caller filters, and reports what it dropped.
   *
   *  Sourced from `obj.facts.annotation_per_contig`, which arrives whole with
   *  the rest of `ObjectDetail` -- never populated by a separate async fetch
   *  after this component mounts. `contig`/`view`'s initial state below is
   *  seeded once on mount rather than re-derived via effect because `drawable`
   *  cannot transition from empty to populated later. */
  contigs: AnnotationContigStat[];
  onPickFeature?: (name: string) => void;
}) {
  const drawable = useMemo(
    () => contigs.filter((c) => typeof c.length === "number" && c.length > 0),
    [contigs],
  );
  const hidden = contigs.length - drawable.length;

  const [contig, setContig] = useState(drawable[0]?.name ?? "");
  const current = drawable.find((c) => c.name === contig) ?? drawable[0];
  const contigLength = current?.length ?? 0;

  const [view, setView] = useState({ start: 0, end: contigLength });
  // Not reset on contig change: a feature-type preference is track-wide, not
  // scoped to one contig, so switching contigs keeps it applied.
  const [typeFilter, setTypeFilter] = useState<string | undefined>();

  const win = useQuery({
    queryKey: ["annotationWindow", obj.id, contig, view.start, view.end, typeFilter],
    queryFn: () =>
      // biotype/strand filtering is deliberately out of scope here -- the API
      // supports both, but this track has no UI control for either yet.
      api.annotationWindow(obj.id, {
        contig,
        start: view.start,
        end: view.end,
        bins: 600,
        feature_type: typeFilter,
      }),
    enabled: Boolean(contig && contigLength > 0),
  });

  function pickContig(name: string) {
    const next = drawable.find((c) => c.name === name);
    setContig(name);
    setView({ start: 0, end: next?.length ?? 0 });
  }

  function zoom(factor: number) {
    const span = view.end - view.start;
    const mid = view.start + span / 2;
    const half = Math.max(50, (span * factor) / 2);
    setView({
      start: Math.max(0, Math.round(mid - half)),
      end: Math.min(contigLength, Math.round(mid + half)),
    });
  }

  function pan(fraction: number) {
    const span = view.end - view.start;
    const delta = Math.round(span * fraction);
    let start = view.start + delta;
    let end = view.end + delta;
    if (start < 0) { end -= start; start = 0; }
    if (end > contigLength) { start -= end - contigLength; end = contigLength; }
    setView({ start: Math.max(0, start), end });
  }

  if (drawable.length === 0) {
    return (
      <div className="section">
        <div className="section-title">Track</div>
        <div className="section-note">
          No contig has a recorded length, so no coordinate axis can be drawn.
          The feature table below is unaffected.
        </div>
      </div>
    );
  }

  const span = Math.max(1, view.end - view.start);
  const toX = (bp: number) =>
    ((Math.min(Math.max(bp, view.start), view.end) - view.start) / span) *
    TRACK_WIDTH;

  const data = win.data;
  const features: AnnotationWindowFeature[] =
    data?.mode === "features" ? data.features : [];
  const maxRow = features.reduce((m, f) => Math.max(m, f.row), 0);
  const bodyHeight = data?.mode === "binned" ? 80 : (maxRow + 1) * ROW_HEIGHT + 12;

  return (
    <div className="section">
      <div className="section-title">Track</div>

      <div className="track-controls">
        <select value={contig} onChange={(e) => pickContig(e.target.value)}>
          {drawable.map((c) => (
            <option key={c.name} value={c.name}>
              {c.name} · {fmt(c.length as number)} bp
            </option>
          ))}
        </select>
        <span className="track-locus">
          {contig}:{fmt(view.start)}-{fmt(view.end)}
        </span>
        <button type="button" className="btn" onClick={() => pan(-0.4)}>←</button>
        <button type="button" className="btn" onClick={() => zoom(0.5)}>+</button>
        <button type="button" className="btn" onClick={() => zoom(2)}>−</button>
        <button
          type="button"
          className="btn"
          onClick={() => setView({ start: 0, end: contigLength })}
        >
          Whole contig
        </button>
        {typeFilter && (
          <button type="button" className="btn" onClick={() => setTypeFilter(undefined)}>
            Clear "{typeFilter}"
          </button>
        )}
      </div>

      {hidden > 0 && (
        <div className="section-note">
          {hidden} contig{hidden === 1 ? "" : "s"} not shown — no recorded
          length, so no axis can be drawn for {hidden === 1 ? "it" : "them"}.
        </div>
      )}

      {win.isError && (
        <div className="section-note">Could not load this region.</div>
      )}

      <svg
        viewBox={`0 0 ${TRACK_WIDTH} ${AXIS_HEIGHT + bodyHeight}`}
        className="annotation-track"
        role="img"
        aria-label={`Annotation features on ${contig} from ${view.start} to ${view.end}`}
      >
        <line
          x1={0} y1={AXIS_HEIGHT - 8} x2={TRACK_WIDTH} y2={AXIS_HEIGHT - 8}
          className="track-axis"
        />
        <text x={0} y={AXIS_HEIGHT - 14} className="track-tick">{fmt(view.start)}</text>
        <text x={TRACK_WIDTH} y={AXIS_HEIGHT - 14} textAnchor="end" className="track-tick">
          {fmt(view.end)}
        </text>

        {data?.mode === "binned" &&
          data.counts.map((count, i) => {
            const max = Math.max(...data.counts, 1);
            const w = TRACK_WIDTH / data.counts.length;
            const h = (count / max) * 70;
            return (
              <rect
                key={i}
                x={i * w}
                y={AXIS_HEIGHT + (70 - h)}
                width={Math.max(1, w - 0.5)}
                height={h}
                className="track-bin"
              >
                <title>{count} features</title>
              </rect>
            );
          })}

        {data?.mode === "features" &&
          features.map((f) => {
            const y = AXIS_HEIGHT + f.row * ROW_HEIGHT;
            const x1 = toX(f.start);
            const x2 = toX(f.end);
            const cls = f.strand === "-" ? "track-feature minus" : "track-feature plus";
            return (
              <g
                key={`${f.feature_id ?? f.name}-${f.start}`}
                className={cls}
                onClick={() => f.name && onPickFeature?.(f.name)}
              >
                <title>
                  {`${f.name ?? "(unnamed)"} · ${f.type ?? "feature"} · ` +
                    `${contig}:${fmt(f.start)}-${fmt(f.end)} · ` +
                    `${f.strand ?? "?"} strand`}
                </title>
                <line x1={x1} y1={y + 7} x2={x2} y2={y + 7} className="track-spine" />
                {f.children.length === 0 ? (
                  <rect x={x1} y={y + 2} width={Math.max(1, x2 - x1)} height={10} rx={2} />
                ) : (
                  f.children.map((c, i) => (
                    <rect
                      key={i}
                      x={toX(c.start)}
                      y={y + 2}
                      width={Math.max(1, toX(c.end) - toX(c.start))}
                      height={10}
                      rx={2}
                    />
                  ))
                )}
              </g>
            );
          })}
      </svg>

      <div className="section-note">
        {win.isLoading && "Loading…"}
        {data?.mode === "binned" &&
          `${fmt(data.total)} features in view — too dense to draw individually. ` +
            `Each bar covers ${fmt(data.bin_bases)} bp. Zoom in to see features.`}
        {data?.mode === "features" &&
          `${features.length} feature${features.length === 1 ? "" : "s"} in view` +
            (data.truncated_rows > 0
              ? ` · +${data.truncated_rows} more not shown — zoom in`
              : "")}
      </div>
    </div>
  );
}
