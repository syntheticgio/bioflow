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
