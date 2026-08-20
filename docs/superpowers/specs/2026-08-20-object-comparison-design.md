# Comparing two objects' visualizations — design

Date: 2026-08-20.

Closes [#645](https://github.com/syntheticgio/bioflow/issues/645). Part of
[#633](https://github.com/syntheticgio/bioflow/issues/633), and the broadest
item in it.

#645 is filed as a specification task because the scope is **a UI concept, not
a chart**, and it names five open questions. This document answers all five and
scopes the narrowest useful version, following the issue's own suggested first
step.

## The gap, restated

For a user iterating on a method, **the comparison is the analysis**: Flye
versus hifiasm, a re-sequenced library versus the original, a trim parameter
changed, a polish step that may have moved duplicated BUSCOs into single-copy
rather than genuinely improving anything. The app can produce both results and
then asks the user to hold one in their head while looking at the other.

## What exists today

Verified against this worktree on 2026-08-20:

- **Selection already lives in the URL.** `DetailPanel` (`DetailPanel.tsx:89`)
  reads `?sel=` and splits it as `kind:id`, dispatching to `ProjectDetail`,
  `ObjectDetail`, or `OperationPanel`. Links across the app set it
  (`PipelineSuggestions.tsx:44`, `ProjectExplorer.tsx`, `SearchView`,
  `DerivedFiles`, `ActivityView`).
- **The charts take plain values, not objects.** `NxChart`'s props are
  `{ curve, totalBases, genomeSize }`; `BuscoChart`'s are four percentages
  plus a total. Neither imports a `DataObject` or fetches anything.
- **`ObjectDetail`** is the component that turns one object's facts into those
  props.
- **`suggestion_service.py`** encodes "what does this operation apply to" for
  cards — prior art for applicability logic, though on the backend.

**This is the finding that shapes everything below.** The charts are already
comparison-ready: they are pure renderers over values. What is coupled to
one object is *the panel that feeds them*, not the charts themselves. The
issue's own assessment — "the charting is the easy half" — is right, and the
code is even better positioned than that suggests.

## Decision C1: selection is a second URL parameter, `cmp`

**How the second object is selected:** a "Compare with…" control on the detail
panel opens a picker scoped to the current project, and the choice is stored
as `?sel=object:A&cmp=object:B`.

Why a URL parameter rather than component state or a pin/multi-select mode:

- **It reuses the seam that already exists.** `sel` is already the single
  source of truth for "what am I looking at", read in one place. `cmp` is the
  same idea with the same lifetime, and `DetailPanel`'s existing dispatch is
  the natural branch point — if `cmp` is present and both parse, render the
  comparison view.
- **It makes the comparison linkable and back-button-correct for free**, which
  answers the persistence question (C5) without building anything.
- A pin mode or explorer multi-select changes how *navigation* works
  everywhere, for a feature whose value is not yet demonstrated. `cmp` changes
  nothing for a user who never opens it.

The picker is scoped to the current project and **filtered to comparable
objects** (C3), so an empty comparison cannot be constructed by selection.

## Decision C2: exactly two, and say so

**Two, not N.** The issue suspects this is right; it is, and the reason is
worth writing down rather than leaving as a scope cut.

Two is not merely simpler to render (two colours, a legend). It is the shape
of the actual question — "is B better than A" — in every motivating case the
issue lists. N-way comparison answers a different question ("which of these
five"), which is a leaderboard rather than an overlay, and would want a table,
not a chart.

So: two, deliberately, with the UI saying so (the control reads "Compare
with…", singular). If N is wanted later it should be designed as the
leaderboard it actually is, not by widening this.

## Decision C3: comparability is a frontend-computed predicate over facts

**What is comparable:** two objects are comparable on a given chart iff both
carry the facts that chart's props require. Not "same format", not "same
role" — the *facts*, because that is the real precondition and it degrades
correctly.

Consequences worth stating:

- Comparability is **per chart, not per object pair**. Two assemblies may
  compare on Nx (both have `sequence_nx_curve`) but not on BUSCO (only one has
  been run through completeness). The right behaviour is to show the Nx
  overlay and say the BUSCO comparison is unavailable because object B has no
  completeness run — **not** to declare the pair incomparable, and not to
  render an empty axis.
- An assembly and a FASTQ share no chart-backing facts, so every chart is
  unavailable and the pair is comparable on nothing. That case must be
  reachable and explained, since a user can construct it deliberately.
- **This lives on the frontend**, beside the panel that already maps facts to
  chart props. It is not `suggestion_service`'s job: that answers "what can I
  *run* on this object", a backend question about tools and preconditions,
  and putting a rendering question there would couple chart props to a service
  that has no business knowing them.

The predicate should be declared as data — a small table of
`{ chartId, requiredFacts[], label }` — rather than as branching inside the
comparison view, so adding a comparable chart is one row and the "why is this
unavailable" message derives from the same table that gates it.

## Decision C4: a dedicated comparison view, not a second series in the panel

**Where it renders:** a distinct comparison view, reached from the detail
panel, not the existing panel with an extra series threaded through.

Why: `ObjectDetail` renders far more than charts — facts tables, provenance,
actions, results tabs. Almost none of that has a two-object meaning ("which
object's Actions tab is this?"), so adding `cmp` to it would mean auditing
every section for a two-object story and inventing one for each. A separate
view renders exactly the comparable charts and nothing else, and cannot
degrade the single-object panel that works today.

The overlay itself: **one axis, two series**, not two charts side by side,
wherever the chart is a curve over a shared X (Nx, QC curves, depth
histogram, the #640 bias curve). A difference of a few percent is visible on
one axis and invisible across two. Where a chart is categorical (BUSCO's four
percentages), paired bars grouped by category serve the same purpose.

## Decision C5: nothing new persists

The URL carries the comparison (C1), so it is shareable, bookmarkable, and
survives a reload with no storage, no schema, and no backend change.

A saved-comparison object would need naming, listing, ownership, and garbage
collection — real cost for a feature whose value is unproven. If users start
pasting comparison URLs around, that is the evidence that would justify it.

**This whole feature is frontend-only.** No new endpoint, no new fact, no
migration. That is a strong reason to build the narrow version and look at it.

## Scope: the narrowest useful version

Following #645's own suggested first step, **stage 1 is two assemblies, Nx
only**, plus the machinery C1–C4 require:

| Stage | Delivers |
|---|---|
| 1 | `cmp` param, comparison view, the comparability table, Nx overlay |
| 2 | Additional charts as rows in the table: BUSCO, QC curves, depth histogram |

Stage 1 is the prototype the issue asks for, but built on the real seams
rather than thrown away — the comparability table and the view are what
stage 2 extends, and stage 2 is one row plus a renderer per chart.

**Stop after stage 1 and look at it.** The open question this design cannot
answer from the code is whether the interaction feels right, and that is
exactly what #645 suspected.

## Requirements

- **R1.** A user viewing an object can choose a second object of the same
  project to compare it with, from a picker that offers only objects sharing
  at least one comparable chart.
- **R2.** The comparison is expressed in the URL and can be shared or
  bookmarked; opening that URL reproduces the comparison.
- **R3.** For each chart in the comparability table, the view renders an
  overlay when both objects carry the required facts, and otherwise states
  which object is missing what.
- **R4.** A pair comparable on nothing renders an explanation, never an empty
  axis.
- **R5.** Curve charts overlay on one axis with a legend naming both objects;
  categorical charts render paired bars.
- **R6.** Removing the comparison returns to the single-object panel unchanged.
- **R7.** Adding a chart to the comparison is one table row plus a renderer;
  no change to the view's control flow.

## Testing

There is no jsdom/testing-library setup, so per CLAUDE.md the pattern is
**pure-function component tests called directly under Vitest**
(`AlignerParamFields.test.tsx`, `ExpressionCharts.test.tsx` are the precedent).

- **The comparability predicate** is a pure function over two fact dicts and
  the table — test it directly: both present → comparable; one missing → not,
  and the message names the object that lacks it; neither → not.
- **Series construction** for the Nx overlay: two curves of different lengths
  produce two paths on one axis without one truncating the other.
- The view itself is verified manually at `http://localhost:5273`.

## Verify before implementing

1. **Does any existing route already use a second search param** in a way that
   would collide with `cmp`?
2. **Is `ObjectDetail`'s facts-to-props mapping extractable** as a pure
   function per chart, or is it entangled with the panel's data fetching? R7
   depends on the answer; if entangled, extracting it is stage 1's real work.
3. **Do the two objects' fact documents require two fetches**, and does the
   existing query layer cache them such that the second is cheap?

## Out of scope

- **N-way comparison** (C2) — a different question wanting a leaderboard.
- **Saved comparisons** (C5) — until URL-sharing proves the demand.
- **Comparing across projects.** Project-scoped, like every other view.
- **Computed difference metrics** ("B's N50 is 12% higher"). The overlay makes
  the difference visible; quantifying it is a further step and would need its
  own decision about which differences are meaningful.
- **Any backend change.** If a comparison turns out to need a fact nothing
  computes, that is a separate issue for that fact.
