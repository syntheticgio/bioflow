# Sequence chart scaffolding design

## Purpose

Refactor the three pointer-driven SVG charts in
`frontend/src/components/SequenceCharts.tsx` to share their repeated layout
and pointer-interaction scaffolding. This addresses
[#227](https://github.com/syntheticgio/bioflow/issues/227) without changing
any user-visible behavior.

`QualityChart`, `GcDistributionChart`, and `NContentChart` currently each
calculate the same plot geometry, construct scale functions, install a
transparent hover target, and render a crosshair/readout interaction. A
future chart with this interaction would otherwise reproduce this shape again.

`BaseCompositionChart` is not part of that shared scaffold: it uses
per-series and per-cycle mouse-enter handlers rather than a plot-wide pointer
target, and it has no matching `pad`/`plotW`/`plotH` or scale setup. It stays
unchanged.

## Scope and compatibility boundary

The existing behavior is the contract. The refactor must preserve each
chart's current dimensions, padding, axis placement, domains, ticks, colors,
reference lines, legend, empty-data behavior, pointer target, hover
selection, crosshair positioning, and readout wording.

This work does not introduce a charting library, change any exported chart
component or prop type, modify `DetailPanel`, or normalize differences among
the charts. Deliberate or incidental differences in snapping and rendering
remain local unless their current markup is exactly equivalent.

## Design

Add an unexported `useChartScaffold` hook in `SequenceCharts.tsx`. The hook
receives the fixed chart dimensions and padding already defined by each
component. It calculates and returns the plot bounds used by all SVG content.

It also centralizes typed hover state, plot-relative pointer coordinate
translation, and the event wiring shared by the transparent interaction
rectangle. A chart supplies its existing datum-selection callback, so the
hook does not decide whether an x coordinate selects a point, a cycle, or a
histogram bin. That preserves chart-specific snapping behavior.

Charts retain ownership of their own data transformations and SVG rendering:

- `QualityChart` retains its quality curve, y-domain, and readout.
- `GcDistributionChart` retains its bins, optional fitted curve, and
  histogram-specific hover behavior.
- `NContentChart` retains its N-content series, threshold line, and readout.

Extract crosshair/readout markup into a small internal helper only if the
existing SVG elements and semantics are identical. Otherwise, leave those
elements within their chart; the hook is sufficient to remove the shared
interaction plumbing without manufacturing a broad renderer API.

## Data flow

1. A chart calls `useChartScaffold` unconditionally with its width, height,
   padding, and data-specific hover resolver, before its current input/empty-
   data early return. This preserves React's hook rules and the present
   early-return behavior.
2. The hook derives plot dimensions and maps pointer events into the plot
   coordinate system.
3. The resolver returns the same datum the pre-refactor chart would have
   selected; the hook stores it as hover state.
4. The chart uses that state and its own scales to render its existing
   crosshair and readout.

The hook must constrain pointer math to the current plot rectangle. No new
runtime error path is needed: malformed or empty values continue to follow
each component's current behavior.

## Verification

Add focused Vitest coverage around the pure extracted behavior: plot geometry
and conversion from browser x coordinates to plot fractions. The frontend has
no DOM component-test harness, so do not add one solely for this maintenance
refactor; chart-level interaction equivalence is verified in the running UI.

Manually verify the three refactored QC cards in the running application with
representative data. Hover at left, middle, and right positions; confirm the
selected datum, crosshair, readout, and mouse-leave clearing match the
pre-refactor UI. Confirm base composition remains unchanged.

## Alternatives considered

### `ChartFrame` render-prop component

A frame component could render the SVG wrapper, interaction rectangle, and
crosshair while charts supplied plot children. It would make the JSX nesting
and types more complex than this single-file maintenance refactor warrants.

### Standalone utilities only

Pure utility functions would reduce repeated scale arithmetic but leave event
wiring and hover state duplicated in every chart. That falls short of the
issue's stated scaffolding extraction goal.

### Normalizing chart differences

Making padding, snapping, or crosshair styles consistent could be worthwhile,
but it changes visible behavior. It is intentionally out of scope and should
be proposed separately.
