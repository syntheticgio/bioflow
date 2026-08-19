import { describe, expect, it } from "vitest";

import {
  SampleCorrelationHeatmap,
  type SampleCorrelation,
} from "./ExpressionCharts";

/**
 * The heatmap's geometry, not its colours.
 *
 * Two things here are easy to get wrong in a way that renders a plausible
 * picture of the wrong data: the reordering that puts replicate blocks on the
 * diagonal, and the cell-to-pair mapping once samples are no longer in matrix
 * order. Both are checked by reading the emitted SVG elements.
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

function render(data: SampleCorrelation) {
  return flatten(SampleCorrelationHeatmap({ data }) as unknown);
}

// Two conditions deliberately interleaved in matrix order, so a component that
// forgot to reorder still produces a picture -- just the wrong one.
const INTERLEAVED: SampleCorrelation = {
  method: "spearman",
  samples: ["a1", "b1", "a2", "b2"],
  conditions: ["alpha", "beta", "alpha", "beta"],
  matrix: [
    [1, 0.5, 0.95, 0.52],
    [0.5, 1, 0.51, 0.96],
    [0.95, 0.51, 1, 0.53],
    [0.52, 0.96, 0.53, 1],
  ],
};

function titles(els: El[]): string[] {
  return els
    .filter((e) => e.type === "title")
    .map((e) => {
      const c = (e.props as { children?: unknown }).children;
      return Array.isArray(c) ? c.join("") : String(c);
    });
}

describe("SampleCorrelationHeatmap", () => {
  it("draws one cell per sample pair", () => {
    const rects = render(INTERLEAVED).filter((e) => e.type === "rect");
    // 16 cells plus the legend swatch.
    expect(rects.length).toBe(17);
  });

  it("groups samples by condition so replicate blocks land on the diagonal", () => {
    const labels = render(INTERLEAVED)
      .filter((e) => e.type === "text")
      .map((e) => String((e.props as { children?: unknown }).children));
    // Row labels come first, in draw order: both alphas before both betas,
    // even though the matrix interleaves them.
    expect(labels.slice(0, 4)).toEqual(["a1", "a2", "b1", "b2"]);
  });

  it("labels each cell with the pair it actually shows", () => {
    const found = titles(render(INTERLEAVED));
    // The cell at grid position (0,1) is a1 vs a2 after reordering, and must
    // carry a1/a2's correlation rather than the raw matrix[0][1].
    expect(found).toContain("a1 vs a2 — spearman 0.950");
    expect(found).toContain("a1 vs b1 — spearman 0.500");
    expect(found).not.toContain("a1 vs a2 — spearman 0.500");
  });

  it("names the correlation method on the figure", () => {
    const labels = render(INTERLEAVED)
      .filter((e) => e.type === "text")
      .map((e) => String((e.props as { children?: unknown }).children));
    expect(labels).toContain("Spearman ρ");
  });

  it("scales colour to the observed range rather than a fixed -1 to 1", () => {
    // Real samples correlate in a narrow band; the extremes of that band must
    // be the extremes of the scale or every cell renders the same shade.
    const labels = render(INTERLEAVED)
      .filter((e) => e.type === "text")
      .map((e) => String((e.props as { children?: unknown }).children));
    expect(labels).toContain("0.960");
    expect(labels).toContain("0.500");
  });

  it("renders nothing when the matrix does not match the sample list", () => {
    expect(
      SampleCorrelationHeatmap({
        data: { ...INTERLEAVED, matrix: [[1, 0.5]] },
      })
    ).toBeNull();
  });

  it("renders nothing for an empty result", () => {
    expect(
      SampleCorrelationHeatmap({
        data: { method: "spearman", samples: [], conditions: [], matrix: [] },
      })
    ).toBeNull();
  });
});
