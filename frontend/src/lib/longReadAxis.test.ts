import { describe, expect, it } from "vitest";
import {
  densityOpacity,
  formatBases,
  formatLength,
  lengthAxis,
  lengthTicks,
} from "./longReadAxis";

describe("lengthAxis", () => {
  it("puts the range endpoints at the plot edges", () => {
    const axis = lengthAxis(100, 100_000, 400, 40);
    expect(axis.x(100)).toBeCloseTo(40);
    expect(axis.x(100_000)).toBeCloseTo(440);
  });

  it("spaces by decade, not linearly", () => {
    // Three decades across 400px is one decade per 133px. Linear spacing
    // would put 1kb at 3px from the left, which is the failure this guards.
    const axis = lengthAxis(100, 100_000, 400, 0);
    expect(axis.x(1_000)).toBeCloseTo(400 / 3, 4);
    expect(axis.x(10_000)).toBeCloseTo((400 * 2) / 3, 4);
  });

  it("survives a run occupying a single bin", () => {
    // Zero-width domain: without the span floor every x is NaN and the chart
    // renders nothing while reporting no error.
    const axis = lengthAxis(5_000, 5_000, 400, 0);
    expect(Number.isFinite(axis.x(5_000))).toBe(true);
    expect(axis.maxLog).toBeGreaterThan(axis.minLog);
  });

  it("does not take log of zero", () => {
    const axis = lengthAxis(0, 1_000, 400, 0);
    expect(Number.isFinite(axis.x(0))).toBe(true);
    expect(Number.isFinite(axis.minLog)).toBe(true);
  });
});

describe("lengthTicks", () => {
  it("labels decades across a typical ONT range", () => {
    const axis = lengthAxis(100, 1_000_000, 400, 0);
    expect(lengthTicks(axis.minLog, axis.maxLog)).toContain(1_000);
    expect(lengthTicks(axis.minLog, axis.maxLog)).toContain(100_000);
  });

  it("returns only ticks inside the range", () => {
    const axis = lengthAxis(1_000, 30_000, 400, 0);
    for (const t of lengthTicks(axis.minLog, axis.maxLog)) {
      expect(t).toBeGreaterThanOrEqual(1_000);
      expect(t).toBeLessThanOrEqual(30_000);
    }
  });

  it("falls back to the endpoints when no candidate fits", () => {
    // A HiFi run occupying 15-18kb contains no decade or third-decade mark,
    // and an unlabelled axis is worse than an unrounded one.
    const axis = lengthAxis(15_000, 18_000, 400, 0);
    expect(lengthTicks(axis.minLog, axis.maxLog).length).toBeGreaterThanOrEqual(2);
  });

  it("does not repeat a tick when the range collapses", () => {
    const ticks = lengthTicks(Math.log10(5_000), Math.log10(5_000));
    expect(new Set(ticks).size).toBe(ticks.length);
  });
});

describe("formatLength", () => {
  it("uses the unit a long-read reader thinks in", () => {
    expect(formatLength(450)).toBe("450bp");
    expect(formatLength(1_000)).toBe("1kb");
    expect(formatLength(15_400)).toBe("15.4kb");
    expect(formatLength(1_000_000)).toBe("1Mb");
  });
});

describe("formatBases", () => {
  it("scales a yield through to gigabases", () => {
    expect(formatBases(800)).toBe("800 bp");
    expect(formatBases(12_092_088)).toBe("12.1 Mb");
    expect(formatBases(4_200_000_000)).toBe("4.20 Gb");
  });
});

describe("densityOpacity", () => {
  it("keeps a single-read cell visible", () => {
    // The sparse tail is the second population the plot exists to show; a
    // linear ramp against a 200,000-read modal cell would render it blank.
    expect(densityOpacity(1, 200_000)).toBeGreaterThan(0.1);
  });

  it("saturates at the busiest cell", () => {
    expect(densityOpacity(500, 500)).toBeCloseTo(1);
  });

  it("is monotonic in the count", () => {
    const a = densityOpacity(10, 1_000);
    const b = densityOpacity(100, 1_000);
    expect(b).toBeGreaterThan(a);
  });

  it("renders an empty cell as nothing rather than a measured zero", () => {
    expect(densityOpacity(0, 1_000)).toBe(0);
  });

  it("handles a grid whose every cell holds one read", () => {
    expect(densityOpacity(1, 1)).toBeCloseTo(1);
  });
});
