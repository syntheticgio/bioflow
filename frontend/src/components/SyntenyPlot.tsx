/**
 * Synteny dot plot: draft assembly (query) against a reference (target).
 *
 * Hand-rolled SVG, same rationale as NxChart/BuscoChart -- a fixed, simple
 * shape where the smallest charting dependency would outweigh the rest of
 * the bundle.
 *
 * Both axes are faceted by contig (point 1): the reference's contigs are
 * concatenated left-to-right on X, the assembly's contigs bottom-to-top on
 * Y, each occupying a span proportional to its *full* length from
 * `target_lengths`/`query_lengths` -- never derived from the segments' own
 * extent, so an unaligned contig still reserves its full band with nothing
 * drawn in it. A thin line marks every contig boundary on both axes, so a
 * boundary reads as "new contig", never as a break in the alignment itself.
 *
 * The Y axis is additionally *ordered* by each query contig's median
 * aligned position on the reference (point 2), not by name or length. A
 * correct, collinear assembly then renders as one continuous diagonal;
 * sorting by name (contig_1, contig_10, contig_2, ...) would shatter that
 * diagonal into unrelated fragments and make a good assembly look wrong for
 * a reason that has nothing to do with the assembly. Query contigs with no
 * segments at all have no median and sort to the end, bottom-most in this
 * component's top-to-bottom Y layout.
 *
 * Segments are colored by strand (point 3): a `-` segment is an inversion,
 * and at whole-genome zoom a short inversion's negative slope is often only
 * a few pixels -- color is what actually makes it legible, not slope.
 */

import { InfoMarker } from "./InfoMarker";

export interface SyntenyAlignment {
  /** Reference object this draft was aligned against, for the aria label. */
  referenceName?: string;
  /**
   * Reference (target) contig name -> full length in bp. Defines the X
   * axis's contig bands. Not derived from segment extent -- a reference
   * contig with zero aligned segments still occupies its full length span.
   */
  targetLengths: Record<string, number>;
  /**
   * Draft assembly (query) contig name -> full length in bp. Defines the Y
   * axis's contig bands, same rationale as targetLengths.
   */
  queryLengths: Record<string, number>;
  /**
   * [targetName, targetStart, targetEnd, queryName, queryStart, queryEnd,
   * strand] tuples, 0-based half-open, already in SVG-ready coordinate
   * order (no conversion needed) -- from `facts.synteny_alignment.segments`.
   */
  segments: [string, number, number, string, number, number, "+" | "-"][];
  /** True when the segment list was capped upstream (minimap2 run limit). */
  segmentsPartial?: boolean;
}

const W = 380;
const H = 380;
const PAD_L = 54;
const PAD_R = 10;
const PAD_T = 10;
const PAD_B = 40;
const PLOT_W = W - PAD_L - PAD_R;
const PLOT_H = H - PAD_T - PAD_B;

const FORWARD_COLOR = "#2e7d32";
const REVERSE_COLOR = "#c62828";

/** Compact base-count label for axis ticks, same shape as NxChart's tick(). */
function tick(n: number): string {
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)} Gb`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} Mb`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)} kb`;
  return `${n} b`;
}

interface Band {
  name: string;
  length: number;
  /** Cumulative offset in the concatenated axis, i.e. this band's start. */
  offset: number;
}

/** Lay out contigs end-to-end in `order`, each occupying `length` of axis space. */
function layoutBands(lengths: Record<string, number>, order: string[]): Band[] {
  let offset = 0;
  const bands: Band[] = [];
  for (const name of order) {
    const length = lengths[name] ?? 0;
    bands.push({ name, length, offset });
    offset += length;
  }
  return bands;
}

export function SyntenyPlot({
  referenceName,
  targetLengths,
  queryLengths,
  segments,
  segmentsPartial,
}: SyntenyAlignment) {
  if (!segments || segments.length === 0) return null;
  if (!targetLengths || !queryLengths) return null;

  const targetNames = Object.keys(targetLengths);
  const queryNames = Object.keys(queryLengths);
  if (targetNames.length === 0 || queryNames.length === 0) return null;

  // X axis: reference contigs in a stable (insertion) order.
  const targetBands = layoutBands(targetLengths, targetNames);
  const targetOffset = new Map(targetBands.map((b) => [b.name, b.offset]));
  const targetTotal = targetBands.reduce((sum, b) => sum + b.length, 0);
  if (targetTotal <= 0) return null;

  // Y axis: query contigs ordered by median absolute target position of
  // their own segments (point 2) -- not name, not length. Contigs with no
  // segments have no median and sort last.
  const medianByQuery = new Map<string, number>();
  const positionsByQuery = new Map<string, number[]>();
  for (const [tName, tStart, tEnd, qName] of segments) {
    const tOff = targetOffset.get(tName);
    if (tOff === undefined) continue;
    const mid = tOff + (tStart + tEnd) / 2;
    if (!positionsByQuery.has(qName)) positionsByQuery.set(qName, []);
    positionsByQuery.get(qName)!.push(mid);
  }
  for (const [qName, positions] of positionsByQuery) {
    const sorted = [...positions].sort((a, b) => a - b);
    const mid = sorted.length / 2;
    const median =
      sorted.length % 2 === 1
        ? sorted[Math.floor(mid)]
        : (sorted[mid - 1] + sorted[mid]) / 2;
    medianByQuery.set(qName, median);
  }
  const orderedQueryNames = [...queryNames].sort((a, b) => {
    const ma = medianByQuery.get(a);
    const mb = medianByQuery.get(b);
    if (ma === undefined && mb === undefined) return a.localeCompare(b);
    if (ma === undefined) return 1; // unaligned contigs sort last
    if (mb === undefined) return -1;
    return ma - mb;
  });

  // Top-to-bottom Y layout: the first-ordered (lowest median) contig sits at
  // the top, consistent with the axis convention below (py() inverts).
  const queryBands = layoutBands(queryLengths, orderedQueryNames);
  const queryOffset = new Map(queryBands.map((b) => [b.name, b.offset]));
  const queryTotal = queryBands.reduce((sum, b) => sum + b.length, 0);
  if (queryTotal <= 0) return null;

  const px = (targetName: string, pos: number) => {
    const off = targetOffset.get(targetName);
    if (off === undefined) return null;
    return PAD_L + ((off + pos) / targetTotal) * PLOT_W;
  };
  // Y grows downward in SVG; row 0 (lowest median) is drawn at the top.
  const py = (queryName: string, pos: number) => {
    const off = queryOffset.get(queryName);
    if (off === undefined) return null;
    return PAD_T + ((off + pos) / queryTotal) * PLOT_H;
  };

  const lines = segments
    .map(([tName, tStart, tEnd, qName, qStart, qEnd, strand], i) => {
      const x1 = px(tName, tStart);
      const x2 = px(tName, tEnd);
      const y1 = py(qName, qStart);
      const y2 = py(qName, qEnd);
      if (x1 === null || x2 === null || y1 === null || y2 === null) return null;
      return { key: i, x1, y1, x2, y2, strand };
    })
    .filter((s): s is NonNullable<typeof s> => s !== null);

  if (lines.length === 0) return null;

  const forwardCount = lines.filter((s) => s.strand === "+").length;
  const reverseCount = lines.length - forwardCount;

  const aria =
    `Synteny alignment against ${referenceName ?? "reference"}: ` +
    `${lines.length} segment${lines.length === 1 ? "" : "s"} ` +
    `(${forwardCount} forward, ${reverseCount} reverse) across ` +
    `${targetBands.length} reference contig${targetBands.length === 1 ? "" : "s"} and ` +
    `${queryBands.length} assembly contig${queryBands.length === 1 ? "" : "s"}`;

  return (
    <div style={{ marginTop: 10 }}>
      <div style={{ fontSize: 11, color: "var(--text-faint)", marginBottom: 4 }}>
        Synteny plot <InfoMarker metric="ui.chart_synteny" />
      </div>
      <svg width={W} height={H} role="img" aria-label={aria}>
        {/* plot border */}
        <rect
          x={PAD_L}
          y={PAD_T}
          width={PLOT_W}
          height={PLOT_H}
          fill="none"
          stroke="var(--border)"
        />
        {/* X (reference) contig boundaries */}
        {targetBands.map((b, i) =>
          i === 0 ? null : (
            <line
              key={`tb-${b.name}`}
              x1={PAD_L + (b.offset / targetTotal) * PLOT_W}
              y1={PAD_T}
              x2={PAD_L + (b.offset / targetTotal) * PLOT_W}
              y2={PAD_T + PLOT_H}
              stroke="var(--border)"
              strokeWidth={0.5}
            />
          ),
        )}
        {/* Y (assembly) contig boundaries */}
        {queryBands.map((b, i) =>
          i === 0 ? null : (
            <line
              key={`qb-${b.name}`}
              x1={PAD_L}
              y1={PAD_T + (b.offset / queryTotal) * PLOT_H}
              x2={PAD_L + PLOT_W}
              y2={PAD_T + (b.offset / queryTotal) * PLOT_H}
              stroke="var(--border)"
              strokeWidth={0.5}
            />
          ),
        )}
        {/* segments, colored by strand */}
        {lines.map((s) => (
          <line
            key={s.key}
            x1={s.x1}
            y1={s.y1}
            x2={s.x2}
            y2={s.y2}
            stroke={s.strand === "+" ? FORWARD_COLOR : REVERSE_COLOR}
            strokeWidth={1.5}
          />
        ))}
        {/* X axis ticks: reference total length */}
        <text
          x={PAD_L}
          y={PAD_T + PLOT_H + 12}
          fontSize={9}
          textAnchor="start"
          fill="var(--text-faint)"
        >
          0
        </text>
        <text
          x={PAD_L + PLOT_W}
          y={PAD_T + PLOT_H + 12}
          fontSize={9}
          textAnchor="end"
          fill="var(--text-faint)"
        >
          {tick(targetTotal)}
        </text>
        <text
          x={PAD_L + PLOT_W / 2}
          y={H - 2}
          fontSize={9}
          textAnchor="middle"
          fill="var(--text-faint)"
        >
          reference{referenceName ? ` (${referenceName})` : ""}
        </text>
        {/* Y axis ticks: assembly total length */}
        <text
          x={PAD_L - 4}
          y={PAD_T + 8}
          fontSize={9}
          textAnchor="end"
          fill="var(--text-faint)"
        >
          0
        </text>
        <text
          x={PAD_L - 4}
          y={PAD_T + PLOT_H}
          fontSize={9}
          textAnchor="end"
          fill="var(--text-faint)"
        >
          {tick(queryTotal)}
        </text>
        <text
          x={12}
          y={PAD_T + PLOT_H / 2}
          fontSize={9}
          textAnchor="middle"
          fill="var(--text-faint)"
          transform={`rotate(-90, 12, ${PAD_T + PLOT_H / 2})`}
        >
          assembly
        </text>
        {/* legend */}
        <g>
          <rect x={PAD_L + 4} y={PAD_T + 4} width={9} height={3} fill={FORWARD_COLOR} />
          <text x={PAD_L + 17} y={PAD_T + 8} fontSize={9} fill="var(--text-faint)">
            forward
          </text>
          <rect x={PAD_L + 62} y={PAD_T + 4} width={9} height={3} fill={REVERSE_COLOR} />
          <text x={PAD_L + 75} y={PAD_T + 8} fontSize={9} fill="var(--text-faint)">
            reverse
          </text>
        </g>
      </svg>
      {segmentsPartial && (
        <div style={{ color: "var(--text-faint)", fontSize: 11, marginTop: 4 }}>
          Showing the {lines.length.toLocaleString()} longest alignment
          segments; shorter ones are omitted.
        </div>
      )}
    </div>
  );
}
