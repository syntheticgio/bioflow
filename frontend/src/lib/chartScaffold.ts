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
