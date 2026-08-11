# Sequence chart scaffolding design

## Purpose

Refactor the four SVG charts in `frontend/src/components/SequenceCharts.tsx`
to share their repeated layout and pointer-interaction scaffolding. This
addresses [#227](https://github.com/syntheticgio/bioflow/issues/227) without
changing any user-visible behavior.

`BaseCompositionChart`, `QualityChart`, `GcDistributionChart`, and
`NContentChart` currently each calculate the same plot geometry, construct
scale functions, install a transparent hover target, and render a
crosshair/readout interaction. A fifth chart would otherwise reproduce this
shape again.

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

- `BaseCompositionChart` retains its four base series and readout.
- `QualityChart` retains its quality curve, y-domain, and readout.
- `GcDistributionChart` retains its bins, optional fitted curve, and
  histogram-specific hover behavior.
- `NContentChart` retains its N-content series, threshold line, and readout.

Extract crosshair/readout markup into a small internal helper only if the
existing SVG elements and semantics are identical. Otherwise, leave those
elements within their chart; the hook is sufficient to remove the shared
interaction plumbing without manufacturing a broad renderer API.

## Data flow

1. A chart validates its input and preserves its present early-return path.
2. The chart calls `useChartScaffold` with its width, height, padding, and
   data-specific hover resolver.
3. The hook derives plot dimensions and maps pointer events into the plot
   coordinate system.
4. The resolver returns the same datum the pre-refactor chart would have
   selected; the hook stores it as hover state.
5. The chart uses that state and its own scales to render its existing
   crosshair and readout.

The hook must constrain pointer math to the current plot rectangle. No new
runtime error path is needed: malformed or empty values continue to follow
each component's current behavior.

## Verification

Add focused frontend tests around the pure extracted behavior: plot geometry,
scale calculations, and pointer-selection boundaries. Preserve or add
chart-level assertions that representative pointer positions select the same
datum and readout values for all four charts.

Manually verify the four QC cards in the running application with
representative data. Hover at left, middle, and right positions; confirm the
selected datum, crosshair, readout, and mouse-leave clearing match the
pre-refactor UI.

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
