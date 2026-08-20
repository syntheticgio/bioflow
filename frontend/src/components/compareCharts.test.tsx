import { describe, expect, it } from "vitest";

import { BuscoBar, BuscoCompareChart, type BuscoSeries } from "./BuscoCompareChart";
import { DepthCompareChart, type DepthSeries } from "./DepthCompareChart";
import { QualityCompareChart, type QcSeries } from "./QualityCompareChart";

/**
 * The comparison renderers are pure components called directly under Vitest
 * (no jsdom), the established pattern. The geometry that matters is the one
 * the single-object chart cannot express and that a comparison gets wrong:
 * the two series must sit on a genuinely shared axis. That is the depth
 * histogram (absolute depth, not bucket index) and the quality curves
 * (shared position domain).
 */

type El = { type?: unknown; props?: Record<string, unknown> };

function flatten(node: unknown, out: El[] = []): El[] {
  if (node == null || typeof node !== "object") return out;
  if (Array.isArray(node)) {
    node.forEach((c) => flatten(c, out));
    return out;
  }
  const el = node as El;
  out.push(el);
  const children = (el.props as { children?: unknown } | undefined)?.children;
  if (children !== undefined) flatten(children, out);
  return out;
}

function rects(node: unknown): { x: number; width: number }[] {
  return flatten(node)
    .filter((e) => e.type === "rect")
    .map((e) => {
      const p = (e.props as { x?: unknown; width?: unknown }) ?? {};
      return { x: Number(p.x ?? 0), width: Number(p.width ?? 0) };
    });
}

function paths(node: unknown): string[] {
  return flatten(node)
    .filter((e) => e.type === "path")
    .map((e) => String((e.props as { d?: unknown }).d ?? ""));
}

function pointsOf(d: string): [number, number][] {
  const pts: [number, number][] = [];
  for (const m of d.matchAll(/[ML]([\d.]+),([\d.]+)/g)) {
    pts.push([Number(m[1]), Number(m[2])]);
  }
  return pts;
}

describe("DepthCompareChart", () => {
  // Two objects with different bucket widths -- the case index-placement
  // gets wrong. A buckets every 10x, B every 30x.
  const a: DepthSeries = {
    name: "A",
    bucketWidth: 10,
    buckets: [
      { depth: 0, count: 100 },
      { depth: 10, count: 50 },
      { depth: 20, count: 25 },
    ],
  };
  const b: DepthSeries = {
    name: "B",
    bucketWidth: 30,
    buckets: [
      { depth: 0, count: 80 },
      { depth: 30, count: 40 },
      { depth: 60, count: 10 },
    ],
  };

  it("places both objects' depth-0 buckets at the same x (absolute depth)", () => {
    const rs = rects(DepthCompareChart({ a, b }));
    const aZero = rs.find((r) => r.x === 34); // PAD.left
    // A's depth-0 and B's depth-0 buckets both start at the left axis.
    expect(aZero).toBeTruthy();
    // There are two rects at x == PAD.left (one per object).
    const atLeft = rs.filter((r) => r.x === 34);
    expect(atLeft.length).toBe(2);
  });

  it("places B's depth-30 bucket past A's depth-10 bucket (not stacked on it)", () => {
    const rs = rects(DepthCompareChart({ a, b }));
    // Collect the bucket x positions (excluding the legend swatches at
    // x=0 and axis-adjacent values). Every distinct left edge belongs to a
    // bucket. Depth-0 buckets sit exactly at PAD.left (34).
    const lefts = rs.map((r) => r.x).filter((x) => x >= 34 && x < 340).sort((x, y) => x - y);
    // A: depth0,10,20 then B: depth0,30,60. Absolute-depth x is depth/maxDepth
    // * plotW. maxDepth = 90 (60+30). PlotW = 360-34-10 = 316.
    // So positions (left edge): A0=34, A10=34+316*10/90≈69, A20≈104,
    // B0=34, B30≈139, B60≈244. Distinct lefts = [34,34,69,104,139,244].
    expect(lefts).toHaveLength(6);
    // B's depth-30 (≈139) must be strictly greater than A's depth-20 (≈104):
    // absolute-depth placement, not index placement (index would give
    // [34,69,104,34,69,104]).
    const a10 = lefts[2];
    const a20 = lefts[3];
    const b30 = lefts[4];
    expect(b30).toBeGreaterThan(a20);
    expect(b30).toBeGreaterThan(a10);
  });
});

describe("QualityCompareChart", () => {
  it("renders two curves on a shared position domain", () => {
    const a: QcSeries = {
      name: "A",
      curve: [
        { position: 1, mean: 35 },
        { position: 100, mean: 30 },
      ],
    };
    const b: QcSeries = {
      name: "B",
      curve: [
        { position: 1, mean: 20 },
        { position: 200, mean: 18 },
      ],
    };
    const ps = paths(QualityCompareChart({ a, b }));
    // Two path series plus... no axis paths, so exactly two paths.
    expect(ps).toHaveLength(2);
    const aPts = pointsOf(ps[0]);
    const bPts = pointsOf(ps[1]);
    // Both start at position 1, so both start at the same x.
    expect(aPts[0][0]).toBe(bPts[0][0]);
    // B extends to position 200, which is the shared max; its last x must be
    // greater than A's last x, and both are on the same 0-42 y scale.
    expect(bPts[bPts.length - 1][0]).toBeGreaterThan(aPts[aPts.length - 1][0]);
  });
});

describe("BuscoCompareChart", () => {
  const a: BuscoSeries = {
    name: "A",
    singlePct: 90,
    duplicatedPct: 5,
    fragmentedPct: 3,
    missingPct: 2,
  };
  const b: BuscoSeries = {
    name: "B",
    singlePct: 70,
    duplicatedPct: 10,
    fragmentedPct: 10,
    missingPct: 10,
  };

  it("renders two paired stacked bars", () => {
    // Each bar is a sub-component, so test its rendered segments directly.
    const aRects = rects(BuscoBar({ series: a, swatch: "#4a9eff" }));
    const bRects = rects(BuscoBar({ series: b, swatch: "#1565c0" }));
    // 4 segments per bar.
    expect(aRects.length).toBe(4);
    expect(bRects.length).toBe(4);
    // A's single-copy (90%) segment is wider than B's (70%).
    expect(aRects[0].width).toBeGreaterThan(bRects[0].width);
    // The container renders two bars.
    const types = flatten(BuscoCompareChart({ a, b })).map((e) => e.type);
    expect(types.filter((t) => t === BuscoBar).length).toBe(2);
  });
});
