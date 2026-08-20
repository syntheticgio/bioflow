# Comparing two objects' visualizations — implementation plan

Date: 2026-08-20.

Closes [#645](https://github.com/syntheticgio/bioflow/issues/645). Companion to
`docs/superpowers/specs/2026-08-20-object-comparison-design.md` (decisions
C1–C5, requirements R1–R7).

**Stage 1 only: two assemblies, Nx curve, plus the machinery.** #645 asks for
the narrowest useful version to be prototyped before generalizing, and this
plan honours that. Stage 2 (BUSCO, QC curves, depth histogram) is one table row
plus a renderer per chart — it does not need a plan, it needs stage 1 to have
been looked at.

**Frontend-only.** No endpoint, no fact, no migration. If something turns out
to need a backend change, that is a signal the design was wrong somewhere —
stop and revisit rather than adding one.

## Spike first

- **S-1. Is `ObjectDetail`'s facts-to-chart-props mapping extractable as a pure
  function per chart?** R7 depends on it. The charts themselves are already
  pure (`NxChart` takes `{curve, totalBases, genomeSize}`; `BuscoChart` takes
  four percentages), so the question is only whether the *mapping* is
  entangled with the panel's fetching. **If it is entangled, extracting it is
  stage 1's real work** and the rest is small.
- **S-2. Does any route already use a second search param** that would collide
  with `cmp`?
- **S-3. Do two objects' fact documents need two fetches, and is the second
  cheap** given the existing query cache?

## Files to touch

| File | Change |
|---|---|
| `frontend/src/lib/comparableCharts.ts` | **New.** The comparability table as data: `{ chartId, requiredFacts[], label }[]`, plus a pure `comparableCharts(factsA, factsB)` returning per-chart availability **and** the reason when unavailable (which object lacks what). This is the C3 predicate and the only real logic in stage 1. |
| `frontend/src/components/DetailPanel.tsx` | Read `cmp` beside the existing `sel` (`DetailPanel.tsx:89` already splits `kind:id`). When both parse to objects, render `ComparisonView`; otherwise unchanged. |
| `frontend/src/components/ComparisonView.tsx` | **New.** Fetches both objects' facts, runs the predicate, renders available overlays and unavailability reasons. |
| `frontend/src/components/NxChart.tsx` | Accept an optional **second** series + legend labels. Keep the single-series call sites working untouched — the NGx clamping logic in `ngxPoints` is subtle and load-bearing; do not restructure it while adding a series. |
| `frontend/src/components/ObjectDetail.tsx` | A "Compare with…" control that opens the picker and sets `cmp`. Picker scoped to the project and filtered to objects sharing ≥1 comparable chart (R1). |
| `frontend/src/lib/comparableCharts.test.ts` | **New.** Pure-function tests, the `ExpressionCharts.test.tsx` pattern. |

## Ordered steps

1. **The comparability table and predicate, first and alone** (C3). Pure
   function, tested directly — there is no jsdom setup, so per CLAUDE.md this
   is the established pattern for frontend logic.
   Three cases, and the second is the one that matters:
   - both objects carry the facts → comparable;
   - **one object lacks them → not comparable *for that chart*, with a message
     naming which object lacks what.** Comparability is per-chart, not per-pair
     (C3): two assemblies may overlay on Nx but not BUSCO because only one has
     a completeness run. Collapsing that to a pair-level verdict would hide the
     Nx comparison the user can have.
   - neither → not comparable, and the pair may be comparable on nothing (R4).
2. **`NxChart` takes an optional second series.** Additive: the existing
   single-series call sites must not change. Test that two curves of different
   lengths both render fully — the failure mode is one truncating the other at
   the shorter curve's end, which looks like a real finding about the assembly
   rather than a rendering bug.
   Leave `ngxPoints` alone. Its clamping and de-duplication logic carries a
   long comment explaining why the curve neither doubles back nor truncates
   early; a refactor while adding a series is how that gets quietly broken.
3. **`ComparisonView`.** Renders one overlay per available chart, and an
   explicit line per unavailable one (R3). The pair-comparable-on-nothing case
   (R4) renders an explanation — **never an empty axis**, which reads as "no
   data in these objects" rather than "these objects share no comparable
   chart".
4. **`DetailPanel` branch** (C1). `cmp` present and parsing → `ComparisonView`.
   Absent or unparseable → exactly today's behaviour, so R6 is structural
   rather than something to remember.
5. **The "Compare with…" control and picker** (R1). Singular wording, per C2 —
   this is deliberately two-object, and the UI should say so rather than
   implying a multi-select that does not exist. Filter the picker to objects
   sharing ≥1 comparable chart, so an empty comparison cannot be *constructed*,
   only reached by hand-editing the URL (which R4 covers).
6. **Manual verification** at `http://localhost:5273` (worktree stack via
   `./ops/worktree-up.sh`), not 5173. Check: two assemblies overlay on Nx; the
   URL round-trips through a reload and the back button (R2); removing the
   comparison returns to the unchanged panel (R6); an assembly-vs-FASTQ pair
   explains itself (R4).

## Then stop

Stage 1 is a prototype built on real seams, not a throwaway — but it is still
a prototype, and **the question it exists to answer is whether the interaction
feels right**, which no amount of code review settles. #645 says this
explicitly and it is the most useful thing in the issue.

Report back with what it looks like rather than continuing into stage 2.

## Verification

```bash
cd frontend && npm test
```

Frontend lint and build run in CI. No backend suite is involved — nothing in
this plan touches `backend/`.

## Out of scope

Per the spec: N-way comparison, saved comparisons, cross-project comparison,
computed difference metrics, and any backend change.
