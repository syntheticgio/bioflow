import { describe, expect, it } from "vitest";
import { lineThroughGaps, plotGeometry, pointerFraction } from "./chartScaffold";

describe("plotGeometry", () => {
  it("keeps the QualityChart layout", () => {
    expect(plotGeometry(460, 210, { top: 10, right: 46, bottom: 26, left: 30 }))
      .toMatchObject({ plotW: 384, plotH: 174 });
  });

  it("keeps the GC and N-content layout", () => {
    expect(plotGeometry(460, 210, { top: 10, right: 16, bottom: 26, left: 38 }))
      .toMatchObject({ plotW: 406, plotH: 174 });
  });
});

describe("pointerFraction", () => {
  it("maps a browser x coordinate relative to the hit rectangle", () => {
    expect(pointerFraction(260, 60, 400)).toBe(0.5);
  });

  it("preserves exact hit-rectangle endpoints", () => {
    expect(pointerFraction(40, 40, 200)).toBe(0);
    expect(pointerFraction(240, 40, 200)).toBe(1);
  });
});

describe("plotFraction", () => {
  // Exercised through the same arithmetic the hook applies, without needing
  // a renderer: a pointer fraction across the whole SVG, re-expressed
  // against a plot area inset by uneven padding.
  const plotFraction = (
    fraction: number,
    width: number,
    pad: { left: number; right: number },
  ) => (fraction * width - pad.left) / (width - pad.left - pad.right);

  it("reproduces the adapter chart's hand-rolled mapping exactly", () => {
    // AdapterContentChart's 96px right gutter holds the legend, so its plot
    // area is much narrower than its viewBox. This is the case that made the
    // raw fraction wrong.
    const pad = { left: 34, right: 96 };
    expect(plotFraction(0, 460, pad)).toBeCloseTo(-0.103, 3);
    expect(plotFraction(0.5, 460, pad)).toBeCloseTo(0.594, 3);
    expect(plotFraction(1, 460, pad)).toBeCloseTo(1.291, 3);
  });

  it("leaves the pointer where it is when there is no padding", () => {
    const pad = { left: 0, right: 0 };
    expect(plotFraction(0.25, 400, pad)).toBe(0.25);
    expect(plotFraction(0.75, 400, pad)).toBe(0.75);
  });

  it("puts the plot area's own edges at 0 and 1", () => {
    // The property that makes this the right correction: a pointer exactly on
    // the left edge of the plot area is fraction 0 of the data, and one on
    // the right edge is fraction 1 -- whatever the padding is.
    const pad = { left: 34, right: 96 };
    expect(plotFraction(34 / 460, 460, pad)).toBeCloseTo(0, 10);
    expect(plotFraction((460 - 96) / 460, 460, pad)).toBeCloseTo(1, 10);
  });
});

describe("lineThroughGaps", () => {
  it("draws one continuous subpath when nothing is missing", () => {
    expect(
      lineThroughGaps([
        { x: 0, y: 10 },
        { x: 1, y: 20 },
        { x: 2, y: 30 },
      ]),
    ).toBe("M 0 10 L 1 20 L 2 30");
  });

  it("ends the line at a trailing gap rather than dropping it to zero", () => {
    expect(
      lineThroughGaps([
        { x: 0, y: 10 },
        { x: 1, y: 20 },
        { x: 2, y: null },
      ]),
    ).toBe("M 0 10 L 1 20");
  });

  it("starts a new subpath after an interior gap instead of bridging it", () => {
    expect(
      lineThroughGaps([
        { x: 0, y: 10 },
        { x: 1, y: null },
        { x: 2, y: 30 },
      ]),
    ).toBe("M 0 10 M 2 30");
  });

  it("is empty when every point is missing", () => {
    expect(lineThroughGaps([{ x: 0, y: null }])).toBe("");
  });
});
