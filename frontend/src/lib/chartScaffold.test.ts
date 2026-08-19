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
