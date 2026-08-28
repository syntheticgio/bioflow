import { useState } from "react";

export interface ChartPadding {
  top: number;
  right: number;
  bottom: number;
  left: number;
}

export interface PlotGeometry {
  width: number;
  height: number;
  pad: ChartPadding;
  plotW: number;
  plotH: number;
}

export function plotGeometry(
  width: number,
  height: number,
  pad: ChartPadding,
): PlotGeometry {
  return {
    width,
    height,
    pad,
    plotW: width - pad.left - pad.right,
    plotH: height - pad.top - pad.bottom,
  };
}

export function pointerFraction(
  clientX: number,
  rectLeft: number,
  rectWidth: number,
): number {
  return (clientX - rectLeft) / rectWidth;
}

/**
 * An SVG path through a series that may have gaps.
 *
 * A null is a position where the value does not exist -- an "after trimming"
 * curve past the point where trimming shortened the read, for instance. Those
 * end the current subpath and start a new one after the gap, rather than
 * being drawn as zero (which reads as a quality collapse) or bridged across
 * (which reads as data that was never measured).
 */
export function lineThroughGaps(
  points: { x: number; y: number | null }[],
): string {
  const segments: string[] = [];
  let open = false;
  for (const point of points) {
    if (point.y == null) {
      open = false;
      continue;
    }
    segments.push(`${open ? "L" : "M"} ${point.x} ${point.y}`);
    open = true;
  }
  return segments.join(" ");
}

/**
 * Geometry plus hover state for a chart whose hover is driven by pointer
 * position along the x axis.
 *
 * Lifted out of SequenceCharts, which had it locally while six other charts
 * hand-rolled the same three lines. `resolveHover` turns a 0..1 fraction of
 * the hit rectangle's width into whatever the chart wants to remember --
 * usually an index, sometimes a tuple -- so the generic parameter carries the
 * chart's own hover type rather than forcing every caller to an index.
 *
 * Charts whose hover comes from a `mouseenter` on an individual bar do not
 * need this: they already know which datum was entered, and routing that
 * through a pointer fraction would be a worse way to say it. They use
 * `plotGeometry` directly.
 */
export function useChartScaffold<T>(
  width: number,
  height: number,
  pad: ChartPadding,
  resolveHover: (
    fraction: number,
    helpers: { plotFraction: (fraction: number) => number },
  ) => T,
) {
  const [hover, setHover] = useState<T | null>(null);
  const geometry = plotGeometry(width, height, pad);

  /**
   * A pointer fraction re-expressed against the plot area rather than the
   * whole SVG.
   *
   * A chart with a wide margin -- a legend gutter, a label column -- has a
   * plot area materially narrower than its viewBox, and mapping the raw
   * fraction onto a data index there lands the hover several positions off.
   * Charts with even padding can ignore this and use the raw fraction.
   */
  const plotFraction = (fraction: number) =>
    (fraction * width - pad.left) / geometry.plotW;

  const onMouseMove = (event: React.MouseEvent<SVGElement>) => {
    const box = event.currentTarget.getBoundingClientRect();
    setHover(
      resolveHover(pointerFraction(event.clientX, box.left, box.width), {
        plotFraction,
      }),
    );
  };

  return {
    ...geometry,
    hover,
    onMouseMove,
    clearHover: () => setHover(null),
  };
}
