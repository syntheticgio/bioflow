/**
 * Axis and formatting maths for the long-read distribution charts.
 *
 * Split out of `LongReadCharts.tsx` because this is the part that can be
 * silently wrong: a chart with a subtly bad axis renders happily and misplaces
 * every bar, where a broken component fails visibly. Extracting it makes the
 * arithmetic testable without a DOM, matching what `chartScaffold.ts` already
 * does for the shared plot geometry.
 */

export interface LengthAxis {
  /** bp to an x coordinate in the SVG's viewBox. */
  x: (length: number) => number;
  minLog: number;
  maxLog: number;
}

/**
 * A log length axis spanning `[start, end]` in bp.
 *
 * Log-spaced because long-read lengths cover orders of magnitude: on a linear
 * axis a run reaching from 200 bp to 100 kb puts everything but the tail
 * against the left edge, the same reason `NxChart.tsx` uses a log Y axis.
 *
 * Both charts build theirs from the same helper so that a feature at 20 kb
 * sits at the same x in each and the pair reads as one picture.
 */
export function lengthAxis(
  start: number,
  end: number,
  plotW: number,
  padLeft: number,
): LengthAxis {
  const minLog = Math.log10(Math.max(start, 1));
  // A run occupying one bin would otherwise give a zero-width domain and
  // divide by zero, putting every bar at NaN.
  const span = Math.max(Math.log10(Math.max(end, 1)) - minLog, 0.1);
  return {
    x: (length: number) =>
      padLeft + ((Math.log10(Math.max(length, 1)) - minLog) / span) * plotW,
    minLog,
    maxLog: minLog + span,
  };
}

/**
 * Decade and third-of-decade ticks lying inside the axis's own range.
 *
 * Falls back to the range's own endpoints when fewer than two candidates fit,
 * so a narrow run is never left with an unlabelled axis -- the same fallback
 * `LengthDistributionChart` makes for the same reason.
 */
export function lengthTicks(minLog: number, maxLog: number): number[] {
  const candidates: number[] = [];
  for (let e = 2; e <= 6; e++) candidates.push(10 ** e, 3 * 10 ** e);

  const inside = candidates.filter(
    (t) => Math.log10(t) >= minLog && Math.log10(t) <= maxLog,
  );
  if (inside.length >= 2) return inside;
  return [...new Set([Math.round(10 ** minLog), Math.round(10 ** maxLog)])];
}

/** A read length, in the unit a reader of long-read data thinks in. */
export function formatLength(bp: number): string {
  if (bp >= 1_000_000)
    return `${(bp / 1_000_000).toFixed(bp % 1_000_000 ? 1 : 0)}Mb`;
  if (bp >= 1_000) return `${(bp / 1_000).toFixed(bp % 1_000 ? 1 : 0)}kb`;
  return `${Math.round(bp)}bp`;
}

/** A base total. Distinct from `formatLength` in spacing and precision: this
 *  is a yield ("1.4 Gb"), not a read length ("1.4kb"). */
export function formatBases(n: number): string {
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)} Gb`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} Mb`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)} kb`;
  return `${n} bp`;
}

/**
 * Opacity for a density cell holding `count` reads, against the grid's
 * busiest cell.
 *
 * Log-compressed: a long-read run's modal cell holds orders of magnitude more
 * reads than its tail, and on a linear ramp every population but the mode
 * renders as blank -- which would hide exactly the second population the
 * density plot exists to reveal. Floored well above zero so a cell holding a
 * single read is still visible.
 */
export function densityOpacity(count: number, max: number): number {
  if (count <= 0) return 0;
  const t = max > 1 ? Math.log10(1 + count) / Math.log10(1 + max) : 1;
  return 0.12 + 0.88 * t;
}
