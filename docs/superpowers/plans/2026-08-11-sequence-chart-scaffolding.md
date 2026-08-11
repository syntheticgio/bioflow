# Sequence chart scaffolding implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove repeated SVG plot geometry and pointer-hover plumbing from `QualityChart`, `GcDistributionChart`, and `NContentChart` without changing their rendered or interactive behavior.

**Architecture:** Put the pure, browser-independent geometry and pointer-fraction operations in a small frontend library module with direct Vitest coverage. Keep the stateful generic `useChartScaffold<T>` hook private to `SequenceCharts.tsx`; each chart supplies its existing datum resolver and retains its scales, SVG series, crosshair markup, labels, and readout.

**Tech Stack:** React 18, TypeScript 5, Vite, Vitest.

## Global Constraints

- Preserve dimensions, padding, domains, axes, ticks, colors, reference lines, hover targets, hover selection, crosshair positions, readout copy, and empty-data returns exactly.
- Refactor only `QualityChart`, `GcDistributionChart`, and `NContentChart`; leave `BaseCompositionChart` unchanged because it uses direct per-element mouse-enter handlers rather than the plot-wide hover pattern.
- Do not add a charting library, change exported chart components or props, modify `DetailPanel`, or normalize any visual or interaction difference.
- Do not add a DOM component-test dependency. This project has Vitest but no jsdom/testing-library harness; cover pure math with Vitest and exercise SVG interaction manually in the worktree UI.
- Run all Docker Compose verification for this worktree through `./ops/worktree-up.sh`, never bare `docker compose`.

---

## File structure

- Create `frontend/src/lib/chartScaffold.ts`: pure `plotGeometry` and `pointerFraction` helpers shared by the local chart hook.
- Create `frontend/src/lib/chartScaffold.test.ts`: direct Vitest cases for all layouts used by the three refactored charts and browser-coordinate fraction conversion.
- Modify `frontend/src/components/SequenceCharts.tsx`: define the private generic hook and adopt it in the three matching charts, leaving their rendering and selection formulas intact.
- Modify `docs/superpowers/specs/2026-08-11-sequence-chart-scaffolding-design.md`: record the discovered three-chart boundary and the repository’s test-harness constraint.

### Task 1: Add tested pure chart geometry primitives

**Files:**
- Create: `frontend/src/lib/chartScaffold.ts`
- Create: `frontend/src/lib/chartScaffold.test.ts`

**Interfaces:**
- Produces `ChartPadding`, `PlotGeometry`, `plotGeometry(width, height, padding)`, and `pointerFraction(clientX, rectLeft, rectWidth)` for `SequenceCharts.tsx`.
- `plotGeometry` returns the original dimensions and padding together with `plotW = width - left - right` and `plotH = height - top - bottom`.
- `pointerFraction` returns `(clientX - rectLeft) / rectWidth` without clamping, matching the charts’ existing event expressions; chart-specific resolvers retain their current clamping or nearest-bin behavior.

- [ ] **Step 1: Write the failing geometry tests**

Create `frontend/src/lib/chartScaffold.test.ts` with the current literal layout values and pointer arithmetic:

```ts
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
});
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run: `cd frontend && npm test -- src/lib/chartScaffold.test.ts`

Expected: FAIL because `./chartScaffold` does not exist.

- [ ] **Step 3: Implement only the pure helpers**

Create `frontend/src/lib/chartScaffold.ts`:

```ts
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
```

- [ ] **Step 4: Run the focused test to verify it passes**

Run: `cd frontend && npm test -- src/lib/chartScaffold.test.ts`

Expected: PASS with three assertions passing.

- [ ] **Step 5: Commit the independently tested helper**

```bash
git add frontend/src/lib/chartScaffold.ts frontend/src/lib/chartScaffold.test.ts
git commit -m "refactor(frontend): add sequence chart geometry helpers"
```

### Task 2: Adopt a private hook in the three matching charts

**Files:**
- Modify: `frontend/src/components/SequenceCharts.tsx:1-772`
- Modify: `frontend/src/lib/chartScaffold.ts`
- Test: `frontend/src/lib/chartScaffold.test.ts`

**Interfaces:**
- Consumes `ChartPadding`, `PlotGeometry`, `plotGeometry`, and `pointerFraction` from `frontend/src/lib/chartScaffold.ts`.
- Produces private `useChartScaffold<T>(width, height, pad, resolveHover)`, returning `geometry`, `hover`, `onMouseMove`, and `clearHover`.
- `resolveHover(fraction: number): T` is supplied by each chart and preserves its existing data-selection expression.

- [ ] **Step 1: Add a regression test for the exact pointer boundary**

Extend `frontend/src/lib/chartScaffold.test.ts` with the boundary values used by the line charts’ existing resolver:

```ts
it("preserves exact hit-rectangle endpoints", () => {
  expect(pointerFraction(40, 40, 200)).toBe(0);
  expect(pointerFraction(240, 40, 200)).toBe(1);
});
```

- [ ] **Step 2: Run the focused test to lock the existing boundary behavior**

Run: `cd frontend && npm test -- src/lib/chartScaffold.test.ts`

Expected: PASS. `pointerFraction` was implemented in Task 1; this regression
case records its endpoint contract before the stateful hook consumes it.

- [ ] **Step 3: Add the private generic hook without changing render ownership**

At the top of `SequenceCharts.tsx`, import the helpers and define the hook after the local data interfaces:

```ts
function useChartScaffold<T>(
  width: number,
  height: number,
  pad: ChartPadding,
  resolveHover: (fraction: number) => T,
) {
  const [hover, setHover] = useState<T | null>(null);
  const geometry = plotGeometry(width, height, pad);

  const onMouseMove = (event: React.MouseEvent<SVGRectElement>) => {
    const box = event.currentTarget.getBoundingClientRect();
    setHover(resolveHover(pointerFraction(event.clientX, box.left, box.width)));
  };

  return {
    ...geometry,
    hover,
    onMouseMove,
    clearHover: () => setHover(null),
  };
}
```

Use `event.currentTarget`, which is the same transparent hit rectangle the
old handlers target, rather than `event.target`; do not move the event handler
to another SVG element.

- [ ] **Step 4: Refactor `QualityChart` while retaining its exact resolver and markup**

Replace its local `useState`, `w`, `h`, `pad`, `plotW`, and `plotH` setup with
the hook. Keep its current `maxPos`, `yMax`, `x`, and `y` expressions. Pass a
resolver equivalent to:

```ts
(fraction) => {
  const idx = Math.round(fraction * (curve.length - 1));
  return curve[Math.max(0, Math.min(curve.length - 1, idx))];
}
```

Wire the existing hit rectangle to `onMouseMove={onMouseMove}` and
`onMouseLeave={clearHover}`. Do not modify its path, axes, dashed crosshair,
or readout text.

- [ ] **Step 5: Refactor `GcDistributionChart` while retaining nearest-bin behavior**

Use the hook with its current dimensions and padding. Preserve the GC-specific
resolver exactly: convert the fraction to `gc = fraction * 100`, then choose
the existing bin whose `gc_percent` has the smallest absolute difference.
Retain its fixed 0–100 x-axis, optional fitted curve, bars, dashed guides,
readout, and all labels. Wire the same hit rectangle to the hook handlers.

- [ ] **Step 6: Refactor `NContentChart` while retaining its exact resolver and markup**

Use the hook with its current dimensions and padding. Preserve the same
rounded-and-clamped curve-index resolver used by `QualityChart`. Leave its
5% reference line, y-domain calculation, axes, dashed crosshair, and readout
unchanged, and wire only its existing transparent hit rectangle to the hook.

- [ ] **Step 7: Confirm `BaseCompositionChart` was not changed**

Inspect the diff and verify that the existing per-series and per-cycle
`onMouseEnter`/`onMouseLeave` handlers at the top of `SequenceCharts.tsx`
remain untouched. It must not import or call the hook.

- [ ] **Step 8: Run static and focused automated verification**

Run:

```bash
cd frontend && npm test -- src/lib/chartScaffold.test.ts
cd frontend && npm run lint
cd frontend && npm run build
```

Expected: the focused Vitest file passes, TypeScript reports no errors, and
the Vite production build completes.

- [ ] **Step 9: Manually verify behavior in the isolated worktree UI**

Run `./ops/worktree-up.sh` from the worktree root and open
`http://localhost:5273`. In a project with QC facts for all charts:

1. Hover at the left, midpoint, and right of `QualityChart`; verify the
   selected cycle, crosshair, and readout match the pre-refactor behavior.
2. Repeat for `GcDistributionChart`, confirming the readout selects the
   nearest GC bin rather than an array index.
3. Repeat for `NContentChart`, confirming its 5% reference line and selected
   cycle remain unchanged.
4. Leave each plot rectangle and verify its crosshair/readout clears.
5. Hover base-composition series and cycles to confirm it remains unchanged.

Stop the isolated stack when finished: `./ops/worktree-up.sh --down`.

- [ ] **Step 10: Commit the behavior-preserving chart refactor**

```bash
git add frontend/src/components/SequenceCharts.tsx frontend/src/lib/chartScaffold.ts frontend/src/lib/chartScaffold.test.ts
git commit -m "refactor(frontend): share sequence chart hover scaffolding"
```

### Task 3: Record the implementation boundary and prepare review

**Files:**
- Modify: `docs/superpowers/specs/2026-08-11-sequence-chart-scaffolding-design.md`

**Interfaces:**
- Consumes the completed implementation and manual verification result from Task 2.
- Produces a design record that explains the three-chart scope and the decision not to add a DOM test harness.

- [ ] **Step 1: Verify the design record matches the landed scope**

Confirm the spec says that base composition uses different per-element hover
handlers, that only the three pointer-driven charts adopt the hook, and that
manual UI verification supplements pure Vitest coverage.

- [ ] **Step 2: Run documentation and worktree checks**

Run:

```bash
git diff --check
git status --short
```

Expected: no whitespace errors and only the intended issue #227 files are
modified or staged.

- [ ] **Step 3: Commit any final spec wording adjustment separately**

If Task 2 revealed a necessary wording change, commit only the spec:

```bash
git add docs/superpowers/specs/2026-08-11-sequence-chart-scaffolding-design.md
git commit -m "docs(frontend): clarify sequence chart refactor scope"
```

If the spec already matches the implementation, do not create an empty
documentation commit.
