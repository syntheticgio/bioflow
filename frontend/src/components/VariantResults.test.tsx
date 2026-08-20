import { describe, expect, it } from "vitest";

import type { VariantSummary } from "../api/types";
import { SubstitutionsTable, substitutionKind } from "./VariantResults";

/**
 * The grouping, not the pixels.
 *
 * The substitution spectrum is banded into transitions and transversions the
 * way a call set is judged, and each band's subtotal is the TSTV count behind
 * the summary row's Ti/Tv statistic -- a second derivation over the rows is
 * exactly what this table exists to remove. Both are checked by reading the
 * emitted rows rather than rendering to a DOM (there is none for the UI).
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

function text(node: unknown): string {
  if (node == null || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number")
    return String(node);
  if (Array.isArray(node)) return node.map(text).join("");
  const el = node as El;
  return text((el.props as { children?: unknown } | undefined)?.children);
}

/** The table's rows in render order, each as its cell texts. */
function tableRows(el: unknown): string[][] {
  return flatten(el)
    .filter((e) => e.type === "tr")
    .map((tr) =>
      flatten(tr)
        .filter((c) => c.type === "td" || c.type === "th")
        .map((cell) => text(cell)),
    );
}

/**
 * All twelve ST classes in bcftools' emit order (verified against bcftools
 * 1.24: every type uses `>` as the separator, transitions and transversions
 * interleaved), with distinct counts.
 */
const INTERLEAVED = [
  { type: "A>C", count: 221 },
  { type: "A>G", count: 1109 },
  { type: "A:T", count: 305 },
  { type: "C>A", count: 210 },
  { type: "C>G", count: 302 },
  { type: "C>T", count: 1097 },
  { type: "G>A", count: 480 },
  { type: "G>C", count: 150 },
  { type: "G:T", count: 230 },
  { type: "T:A", count: 141 },
  { type: "T>C", count: 460 },
  { type: "T:G", count: 161 },
];

const SUMMARY = { ts: 3146, tv: 2281 } as unknown as VariantSummary;

describe("substitutionKind", () => {
  it("classifies the twelve ST classes bcftools emits", () => {
    for (const t of ["A>G","C>T","G>A","T>C"]) {
      expect(substitutionKind(t), t).toBe("transition");
    }
    for (const t of ["A>C","A:T","C>A","C>G","G>C","G:T","T:A","T:G"]) {
      expect(substitutionKind(t), t).toBe("transversion");
    }
  });

  it("tolerates a `:` separator and lowercase bases", () => {
    // The stored facts are bcftools' raw strings; older releases used `:`
    // for some classes, so the classification must not hinge on the
    // separator.
    expect(substitutionKind("A:G")).toBe("transition");
    expect(substitutionKind("a>g")).toBe("transition");
    expect(substitutionKind("C:A")).toBe("transversion");
    expect(substitutionKind("c:t")).toBe("transition");
  });

  it("returns null for a type that is not a single-base change", () => {
    for (const t of ["I", "D", ".", "A", "A>T>", "AT>GC", ""]) {
      expect(substitutionKind(t)).toBeNull();
    }
  });
});

describe("SubstitutionsTable", () => {
  it("bands transitions together first, then transversions", () => {
    const rows = tableRows(SubstitutionsTable({ rows: INTERLEAVED, summary: SUMMARY }));
    // Header row, then the two group headers in order.
    expect(rows[0]).toEqual(["Type", "Count", ""]);
    expect(rows[1][0]).toBe("Transitions");
    const transitionTypes = rows.slice(2, 2 + 4).map((r) => r[0]);
    expect(transitionTypes).toEqual(["A>G","C>T","G>A","T>C"]);
    expect(rows[6][0]).toBe("Transversions");
    // Within each group the bcftools order is kept.
    const transversionTypes = rows.slice(7, 7 + 8).map((r) => r[0]);
    expect(transversionTypes).toEqual(["A>C","A:T","C>A","C>G","G>C","G:T","T:A","T:G"]);
    expect(rows.length).toBe(1 + 1 + 4 + 1 + 8);
  });

  it("takes the group subtotals from the TSTV counts, not from the rows", () => {
    // The rows sum to 3119 transitions and 1580 transversions -- the
    // subtotals must still be the summary's 3146 and 2281, the same counts
    // the Ti/Tv statistic divides.
    const rows = tableRows(
      SubstitutionsTable({
        rows: [
          { type: "A>G", count: 2000 },
          { type: "T:C", count: 1119 },
          { type: "A:C", count: 1580 },
        ],
        summary: { ts: 3146, tv: 2281 } as unknown as VariantSummary,
      }),
    );
    // Layout: header, Transitions header, A>G, T:C, Transversions header,
    // A:C.
    expect(rows[1]).toEqual(["Transitions", (3146).toLocaleString(), ""]);
    expect(rows[4]).toEqual(["Transversions", (2281).toLocaleString(), ""]);
  });

  it("falls back to the row sums when no summary is present", () => {
    const rows = tableRows(
      SubstitutionsTable({
        rows: [
          { type: "A>G", count: 2000 },
          { type: "T:C", count: 1119 },
          { type: "A:C", count: 1580 },
        ],
      }),
    );
    expect(rows[1][1]).toBe((3119).toLocaleString());
    expect(rows[4][1]).toBe((1580).toLocaleString());
  });

  it("keeps every row's own count", () => {
    const rows = tableRows(SubstitutionsTable({ rows: INTERLEAVED, summary: SUMMARY }));
    const byType = new Map(rows.slice(1).map((r) => [r[0], r[1]]));
    for (const r of INTERLEAVED) {
      expect(byType.get(r.type)).toBe(r.count.toLocaleString());
    }
  });

  it("colours the two groups differently", () => {
    const els = flatten(SubstitutionsTable({ rows: INTERLEAVED, summary: SUMMARY }));
    const bars = els.filter(
      (e) =>
        e.type === "div" &&
        (e.props as { style?: { background?: string } })?.style?.background,
    );
    const colours = new Set(
      bars.map((e) => (e.props as { style: { background: string } }).style.background),
    );
    // One colour per group, nothing else.
    expect(colours).toEqual(new Set(["var(--accent)", "var(--warn)"]));
  });

  it("renders an unparseable ST type without error or a group", () => {
    const rows = tableRows(
      SubstitutionsTable({
        rows: [
          { type: "A>G", count: 10 },
          { type: "AT>GC", count: 3 },
        ],
        summary: SUMMARY,
      }),
    );
    expect(rows.map((r) => r[0])).toEqual([
      "Type",
      "Transitions",
      "A>G",
      "Transversions",
      "AT>GC",
    ]);
  });

  it("still renders when every row is unparseable", () => {
    const rows = tableRows(
      SubstitutionsTable({
        rows: [{ type: "???", count: 7 }],
        summary: SUMMARY,
      }),
    );
    // Header + two group headers + the row itself.
    expect(rows).toHaveLength(4);
    expect(rows[3][0]).toBe("???");
  });

  it("caps the group bar at the column width", () => {
    // The group total exceeds the largest row, so its bar must not overflow
    // the 40% bar column.
    const els = flatten(
      SubstitutionsTable({
        rows: [
          { type: "A>G", count: 10 },
          { type: "A:C", count: 10 },
        ],
        summary: { ts: 100, tv: 100 } as unknown as VariantSummary,
      }),
    );
    const widths = els
      .filter(
        (e) =>
          e.type === "div" &&
          typeof (e.props as { style?: { width?: string } })?.style?.width ===
            "string",
      )
      .map((e) => (e.props as { style: { width: string } }).style.width);
    expect(widths.length).toBeGreaterThan(0);
    expect(widths.every((w) => parseFloat(w) <= 100)).toBe(true);
  });
});
