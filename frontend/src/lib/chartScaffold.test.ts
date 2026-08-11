import { describe, expect, it } from "vitest";
import { plotGeometry, pointerFraction } from "./chartScaffold";

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
