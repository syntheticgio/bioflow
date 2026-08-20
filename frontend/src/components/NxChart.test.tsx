import { describe, expect, it } from "vitest";

import { NxChart } from "./NxChart";

/**
 * The comparison overlay's geometry: two curves of different lengths must
 * both render fully on the shared axis. The failure mode is the y scale
 * tracking only the primary curve, which pushes the second curve's longer
 * extent off the top of the plot -- visually indistinguishable from a real
 * finding about the assembly. Tested directly (no jsdom), the established
 * pattern for pure chart components here.
 */

const PAD_T = 12;
const PAD_B = 30;
const PLOT_H = 180 - PAD_T - PAD_B;

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

/** Every `<path>` element's `d` string, for the series drawn as paths. */
function paths(node: unknown): string[] {
  return flatten(node)
    .filter((e) => e.type === "path")
    .map((e) => String((e.props as { d?: unknown }).d ?? ""));
}

/** All (x, y) points named by an SVG path `d` string (M/L commands only). */
function pointsOf(d: string): [number, number][] {
  const pts: [number, number][] = [];
  for (const m of d.matchAll(/[ML]([\d.]+),([\d.]+)/g)) {
    pts.push([Number(m[1]), Number(m[2])]);
  }
  return pts;
}

const PRIMARY = {
  curve: [
    [1, 100],
    [50, 1000],
    [100, 5000],
  ] as [number, number][],
  totalBases: 200_000,
  label: "Assembly A",
};

const LONGER = {
  curve: [
    [1, 50],
    [50, 200],
    [100, 20_000],
  ] as [number, number][],
  totalBases: 500_000,
  label: "Assembly B",
};

describe("NxChart", () => {
  it("returns null for an empty curve, unchanged by the compare prop", () => {
    expect(NxChart({ curve: [], totalBases: 1000, compare: LONGER })).toBeNull();
  });

  it("draws both the primary and comparison curves", () => {
    const tree = NxChart({ ...PRIMARY, compare: LONGER }) as unknown;
    // Primary Nx + comparison Nx = two paths. No NGx (no genomeSize on either).
    expect(paths(tree)).toHaveLength(2);
  });

  it("spans the y scale across both curves so the longer comparison is not clipped", () => {
    const tree = NxChart({ ...PRIMARY, compare: LONGER }) as unknown;
    const d = paths(tree)[1]; // comparison curve renders after the primary
    const ys = pointsOf(d).map(([, y]) => y);
    // Every comparison point must sit inside the plot band. If the scale had
    // been derived from the primary curve alone, its 20,000 bp peak would map
    // above PAD_T (y < 12).
    expect(Math.min(...ys)).toBeGreaterThanOrEqual(PAD_T - 0.01);
    expect(Math.max(...ys)).toBeLessThanOrEqual(PAD_T + PLOT_H + 0.01);
  });

  it("keeps the primary curve fully inside the plot when the comparison is shorter", () => {
    const tree = NxChart({ ...PRIMARY, compare: LONGER }) as unknown;
    const d = paths(tree)[0];
    const ys = pointsOf(d).map(([, y]) => y);
    expect(Math.min(...ys)).toBeGreaterThanOrEqual(PAD_T - 0.01);
    expect(Math.max(...ys)).toBeLessThanOrEqual(PAD_T + PLOT_H + 0.01);
  });

  it("labels both series in the legend when comparing", () => {
    const tree = NxChart({ ...PRIMARY, compare: LONGER }) as unknown;
    const labels = flatten(tree)
      .filter((e) => e.type === "text")
      .map((e) => {
        const c = (e.props as { children?: unknown }).children;
        return Array.isArray(c) ? c.join("") : String(c);
      });
    expect(labels).toContain("Assembly A");
    expect(labels).toContain("Assembly B");
  });
});
