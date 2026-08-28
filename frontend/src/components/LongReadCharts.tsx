import { useState } from "react";

import { plotGeometry } from "../lib/chartScaffold";
import { InfoMarker } from "./InfoMarker";
import {
  densityOpacity,
  formatBases,
  formatLength,
  lengthAxis,
  lengthTicks,
} from "../lib/longReadAxis";
import type { QcFacts } from "../api/types";

/**
 * The two long-read QC distributions, from the per-read data NanoPlot's
 * `--raw` output carries and the scalar summary discards.
 *
 * Hand-rolled SVG over a precomputed grid, following `SequenceCharts.tsx`'s
 * convention and for the same reason: the backend has already binned these,
 * so the layout is known and a charting dependency would only replace
 * arithmetic. The density plot stretches that convention in needing a colour
 * scale, but it is still a fixed shape over fixed cells -- the same shape
 * `TileQualityChart` draws, whose `absoluteColor` ramp this deliberately does
 * not reuse, since that one encodes Phred quality and this one encodes read
 * density on an axis where quality is a coordinate rather than the value.
 *
 * Both self-suppress when their fact is absent, which is how a long-read file
 * QC'd before these shipped renders exactly the QC tab it did before.
 *
 * Kept out of `SequenceCharts.tsx` because these are fed by the NanoPlot path
 * rather than the sampled short-read/FASTA scan every chart in that file
 * reads, and it is already the longest component file in this directory.
 */

const W = 460;
const H = 210;

/**
 * Total bases per read-length bin.
 *
 * The Y axis is bases, not reads, and that distinction is the whole point of
 * the chart -- see the `InfoMarker` copy, which says so on the page too,
 * because a reader who assumes read counts will draw the opposite conclusion
 * from the same picture. An ONT run's reads are mostly short while its bases
 * are mostly long; only the base-weighted view predicts whether a repeat gets
 * spanned, and it is the distribution N50 summarises into one number.
 *
 * N50 is drawn on the axis when it is known, tying that scalar -- already in
 * the key-value list directly above -- to the shape it came from.
 */
export function LengthBasesHistogram({
  histogram,
  n50,
}: {
  histogram: NonNullable<QcFacts["qc_length_bases_histogram"]>;
  n50?: number | null;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const bins = histogram.bins;
  if (!bins?.length) return null;

  const { pad, plotW, plotH } = plotGeometry(W, H, {
    top: 10,
    right: 14,
    bottom: 26,
    left: 44,
  });

  const axis = lengthAxis(
    bins[0].length_bin,
    bins[bins.length - 1].length_bin_end,
    plotW,
    pad.left,
  );
  const maxBases = Math.max(...bins.map((b) => b.bases));
  const y = (bases: number) => pad.top + plotH - (bases / maxBases) * plotH;

  const ticks = lengthTicks(axis.minLog, axis.maxLog);
  // Inside the axis only: an N50 past the last occupied bin would draw a line
  // hanging off the plot, and the number is on the page regardless.
  const n50Inside =
    n50 != null &&
    Math.log10(n50) >= axis.minLog &&
    Math.log10(n50) <= axis.maxLog
      ? n50
      : null;

  const hovered = hover == null ? null : bins[hover];

  return (
    <div>
      <svg
        width="100%"
        viewBox={`0 0 ${W} ${H}`}
        style={{ maxWidth: W, display: "block" }}
        onMouseLeave={() => setHover(null)}
      >
        <line
          x1={pad.left}
          x2={W - pad.right}
          y1={pad.top + plotH}
          y2={pad.top + plotH}
          stroke="var(--border)"
          strokeWidth="1"
        />

        {/* Each bar spans its own bin's width on the log axis rather than a
            shared average: log-spaced bins are equal-width in log space, so
            this is uniform here, but deriving it from the bin's own edges
            keeps the drawing correct if the binning is ever retuned. */}
        {bins.map((b, i) => {
          const x0 = axis.x(b.length_bin);
          const x1 = axis.x(b.length_bin_end);
          return (
            <rect
              key={b.length_bin}
              x={x0}
              y={y(b.bases)}
              width={Math.max(1, x1 - x0 - 1)}
              height={pad.top + plotH - y(b.bases)}
              fill="var(--accent)"
              opacity={hover === i ? 0.9 : 0.5}
              onMouseEnter={() => setHover(i)}
            />
          );
        })}

        {n50Inside != null && (
          <>
            <line
              x1={axis.x(n50Inside)}
              x2={axis.x(n50Inside)}
              y1={pad.top}
              y2={pad.top + plotH}
              stroke="var(--text-faint)"
              strokeWidth="1"
              strokeDasharray="3 3"
            />
            <text
              x={axis.x(n50Inside) + 3}
              y={pad.top + 9}
              fontSize="9"
              fill="var(--text-faint)"
            >
              N50
            </text>
          </>
        )}

        {ticks.map((t) => (
          <text
            key={t}
            x={axis.x(t)}
            y={H - 6}
            textAnchor="middle"
            fontSize="9"
            fill="var(--text-faint)"
          >
            {formatLength(t)}
          </text>
        ))}

        <text
          x={4}
          y={pad.top + 8}
          fontSize="9"
          fill="var(--text-faint)"
        >
          bases
        </text>
      </svg>

      <div style={{ fontSize: 11, color: "var(--text-faint)", marginTop: 2 }}>
        {hovered
          ? `${formatLength(hovered.length_bin)}–${formatLength(hovered.length_bin_end)}: ${formatBases(hovered.bases)} in ${hovered.reads.toLocaleString()} reads`
          : `${formatBases(histogram.total_bases)} across ${histogram.total_reads.toLocaleString()} reads · hover for detail`}
      </div>
    </div>
  );
}

/** One ink at varying opacity rather than a multi-hue ramp, so an empty cell
 *  is genuinely absent rather than a dark cell that reads as a measured zero.
 *  Deliberately not the quality ramp `TileQualityChart` uses -- there the
 *  colour *is* the quality, here quality is an axis and the colour counts
 *  reads, and reusing a red-to-green quality ramp would say a dense cell was
 *  a good one. */
function densityColor(count: number, max: number): string {
  return `rgba(88, 166, 255, ${densityOpacity(count, max).toFixed(3)})`;
}

/**
 * Read length against read quality, as a density grid.
 *
 * The 2D view separates failure modes the scalar means cannot: a cloud of
 * short low-quality reads dragging the mean down versus a run that is
 * uniformly mediocre (same mean length, same mean quality, opposite
 * remedies), or a HiFi run with a CLR-like second population sitting at lower
 * quality. It also answers whether the long reads an assembly needs are the
 * good ones or whether length and quality are anticorrelated in this run.
 *
 * Log length axis for the same reason the histogram above uses one: linear,
 * every real run piles against the left edge.
 */
export function LengthQualityDensityChart({
  density,
}: {
  density: NonNullable<QcFacts["qc_length_quality_density"]>;
}) {
  const [hover, setHover] = useState<[number, number, number] | null>(null);
  const cells = density.cells;
  if (!cells?.length) return null;

  const { pad, plotW, plotH } = plotGeometry(W, H, {
    top: 10,
    right: 14,
    bottom: 26,
    left: 30,
  });

  const lengthStarts = [...new Set(cells.map((c) => c[0]))].sort((a, b) => a - b);
  // Bin width in log space is fixed by the backend's bins-per-decade, so the
  // last bin's end is derivable rather than needing to be carried per cell.
  const binFactor = 10 ** (1 / density.bins_per_decade);
  const axis = lengthAxis(
    lengthStarts[0],
    lengthStarts[lengthStarts.length - 1] * binFactor,
    plotW,
    pad.left,
  );

  // Quality axis spans only the occupied range rather than the full 0-50 the
  // backend allows: an ONT run occupies Q8-Q20 and stretching the axis to
  // Q50 for it would squash the whole picture into a fifth of the plot.
  const qualities = cells.map((c) => c[1]);
  const qMin = Math.max(0, Math.min(...qualities) - 1);
  const qMax = Math.max(...qualities) + 2;
  const y = (q: number) => pad.top + plotH - ((q - qMin) / (qMax - qMin)) * plotH;
  const cellH = Math.max(1, (plotH / (qMax - qMin)) - 0.5);

  const ticks = lengthTicks(axis.minLog, axis.maxLog);
  const qTicks: number[] = [];
  for (let q = Math.ceil(qMin / 5) * 5; q <= qMax; q += 5) qTicks.push(q);

  return (
    <div>
      <svg
        width="100%"
        viewBox={`0 0 ${W} ${H}`}
        style={{ maxWidth: W, display: "block" }}
        onMouseLeave={() => setHover(null)}
      >
        {qTicks.map((q) => (
          <g key={q}>
            <line
              x1={pad.left}
              x2={W - pad.right}
              y1={y(q)}
              y2={y(q)}
              stroke="var(--border)"
              strokeWidth="0.5"
              opacity={0.5}
            />
            <text
              x={pad.left - 4}
              y={y(q) + 3}
              textAnchor="end"
              fontSize="9"
              fill="var(--text-faint)"
            >
              Q{q}
            </text>
          </g>
        ))}

        {cells.map(([lengthStart, q, count]) => {
          const x0 = axis.x(lengthStart);
          const x1 = axis.x(lengthStart * binFactor);
          return (
            <rect
              key={`${lengthStart}-${q}`}
              x={x0}
              y={y(q + 1)}
              width={Math.max(1, x1 - x0)}
              height={cellH}
              fill={densityColor(count, density.max_count)}
              onMouseEnter={() => setHover([lengthStart, q, count])}
            />
          );
        })}

        <line
          x1={pad.left}
          x2={W - pad.right}
          y1={pad.top + plotH}
          y2={pad.top + plotH}
          stroke="var(--border)"
          strokeWidth="1"
        />

        {ticks.map((t) => (
          <text
            key={t}
            x={axis.x(t)}
            y={H - 6}
            textAnchor="middle"
            fontSize="9"
            fill="var(--text-faint)"
          >
            {formatLength(t)}
          </text>
        ))}
      </svg>

      <div style={{ fontSize: 11, color: "var(--text-faint)", marginTop: 2 }}>
        {hover
          ? `${formatLength(hover[0])}–${formatLength(Math.round(hover[0] * binFactor))} at Q${hover[1]}–Q${hover[1] + 1}: ${hover[2].toLocaleString()} reads`
          : `${density.total_reads.toLocaleString()} reads · darker is denser · hover for detail`}
      </div>
    </div>
  );
}

/**
 * Both distributions, or nothing.
 *
 * One component rather than two mounted separately so the QC tab has a single
 * place that knows these belong together: they come from one pass over one
 * file and share a length axis, and a run old enough to have neither should
 * render nothing rather than an empty heading.
 *
 * A plain block rather than its own `.section`: this mounts *inside* the QC
 * report's section, and `FactsColumns` measures `:scope > .facts-group,
 * .section` to pack its columns -- a nested one would be measured as a
 * separate card and carry a second card's margin inside the first.
 */
export function LongReadDistributions({ qc }: { qc: QcFacts }) {
  const histogram = qc.qc_length_bases_histogram;
  const density = qc.qc_length_quality_density;
  if (!histogram?.bins?.length && !density?.cells?.length) return null;

  return (
    <div style={{ marginTop: 14 }}>

      {histogram?.bins?.length ? (
        <div style={{ marginBottom: 14 }}>
          <div style={{ fontSize: 11, marginBottom: 4 }}>
            Bases by read length
            <InfoMarker metric="ui.chart_length_bases_histogram" />
          </div>
          <LengthBasesHistogram
            histogram={histogram}
            n50={qc.qc_read_length_n50}
          />
        </div>
      ) : null}

      {density?.cells?.length ? (
        <div>
          <div style={{ fontSize: 11, marginBottom: 4 }}>
            Length vs quality
            <InfoMarker metric="ui.chart_length_quality_density" />
          </div>
          <LengthQualityDensityChart density={density} />
        </div>
      ) : null}
    </div>
  );
}
