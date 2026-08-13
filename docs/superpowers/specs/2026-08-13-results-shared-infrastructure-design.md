# Shared infrastructure across file result views

Design for [#299](https://github.com/syntheticgio/bioflow/issues/299), a
follow-up to [#257](https://github.com/syntheticgio/bioflow/issues/257).

## Problem

The BAM, VCF, differential-expression, and annotation Results views were built
in sequence, each reusing the previous one's shape by hand. #257 deliberately
took that path rather than introducing a broad abstraction. This design walks
what duplication actually emerged, and extracts only the parts that have at
least two concrete consumers and a stable interface.

## What the inventory found

Three candidate areas, of which two are worth extracting.

### Report path containment (extract)

`get_bam_stats_report` and `get_vcf_stats_report` each hand-roll the same
three-step guard: reject `..`, empty, and absolute segments; resolve; re-check
the resolved path against the report root. Step three is spelled differently in
the two:

| Endpoint | Predicate |
|---|---|
| `get_bam_stats_report` | `target.is_relative_to(root)` |
| `get_vcf_stats_report` | `root not in target.parents` |

**These are not equivalent predicates, but they agree on every input that
reaches them.** `is_relative_to` accepts `root` itself where `parents` does
not; that divergence is unreachable because both call sites `and` in
`.is_file()`, and `root` is a directory. The one input that would separate them
at the resolve step (`sub/../a.tsv`) is rejected by the prefilter first.

This was verified directly against the two predicates rather than reasoned
about:

| `report_path` | prefilter | BAM predicate | VCF predicate |
|---|---|---|---|
| `a.tsv` | pass | True | True |
| `sub/b.tsv` | pass | True | True |
| `sub/../a.tsv` | BLOCK | — | — |
| `./a.tsv` | pass | True | True |
| `''` | pass | False | False |

So this is **stylistic drift, not a live vulnerability**, and the extraction is
behavior-preserving. The reason to do it anyway is forward-looking: the guard is
currently correct twice by hand, with `.is_file()` silently load-bearing in both
spellings. A third result type that copies the weaker-looking half, or drops the
`.is_file()`, inherits a hole. One helper makes the guard inheritable instead of
re-derivable.

### Frontend compute lifecycle (extract)

`BamResults`, `VariantResults`, and `AnnotationResults` each define the same
mutation:

```
useMutation → invalidateQueries(["jobs"]) → notify.info("Computing results")
```

alongside a near-identical `NodeSelector` + "Compute results" empty state and an
inline-styled "recompute results" button repeated with the same five style
properties.

`Stat` is defined twice — `BamResults.tsx:294` and `VariantResults.tsx:243` —
with identical props and **different styling**: BAM renders a 10px label with a
600-weight value, VCF renders an 11px uppercase tracked label with a 22px value.
That is unintended visual drift between two views a user reads as a set.

### Result tables (do not extract)

`ContigTable` (128 lines), `VariantTable` (413), and `AnnotationFeatureTable`
(593) appear duplicated and are not. `ContigTable` pages naively over a TSV. The
other two share perhaps 30 lines of `page`/`lastTotal`/`skipCount` bookkeeping;
their bulk is format-specific — variant context and structure popups, hierarchy
expansion, a three-way view toggle.

`AnnotationFeatureTable`'s own docstring records that it mirrored
`VariantTable`'s conventions on purpose. That is convergence for consistency,
which is cheap, not duplication that costs anything to maintain.

Extracting a `<ResultTable>` here would couple ~1,000 lines of unlike behavior
to save ~30, which is the "force unlike result types into one schema" the issue
names as a non-goal. **Recorded as a deliberate rejection so it is not
re-proposed.**

### Backend index-path preamble (deferred, not in this design)

Five endpoints — variants, features, genes, children, window — repeat a
resolve-owner / build-`db_path` / 404 preamble, and each re-explains the same
security rule in its docstring: the stats directory is keyed by object id alone,
so the ownership lookup is the only thing separating profiles.

Considered and **deliberately dropped from this design**: it touches five
endpoints to save roughly six lines each, and the repetition is primarily in the
docstrings rather than the logic. That is a documentation problem, and forcing a
code abstraction onto it trades a wide blast radius for little. Left as-is.

## Design

Two units. Each has at least two consumers and an interface that does not leak
its callers' specifics.

### Unit 1 — `resolve_report_file()`

A backend helper taking a report root and a client-supplied relative path,
returning the resolved file or raising `NotFoundError`.

It encodes all three steps in one place, adopting the stricter spelling
(`is_relative_to` plus an explicit `is_file` check) so containment does not
depend on `.is_file()` being incidentally load-bearing.

**Consumers:** `get_bam_stats_report`, `get_vcf_stats_report`.

`get_qc_report` is a candidate but carries additional CSP and HTML-rendering
concerns. It is left alone unless it drops out cleanly, so the diff stays
honest about what it changes.

**Behavior:** unchanged, per the verification table above.

### Unit 2 — `useComputeResults()` and a shared `Stat`

A frontend hook wrapping the mutation triad and returning the mutation plus its
pending state, and one shared `Stat` component.

**Consumers:** `BamResults`, `VariantResults`, `AnnotationResults` for the hook;
`BamResults` and `VariantResults` for `Stat`.

`ExpressionResults` is **not** a `Stat` consumer. It renders a `facts-table` of
`<th>`/`<td>` rows rather than stat tiles. Converting it would be a redesign,
not a refactor, and is out of scope.

**One visible change:** consolidating `Stat` forces choosing one of the two
current treatments. This design takes the `VariantResults` styling — the larger
value and uppercase tracked label — as the intended direction, being the newer
of the two. This is the only user-visible pixel change in the work and is
isolated in its own commit for that reason.

## Testing

The existing traversal suites in `test_bam_stats_reports.py` and
`test_vcf_stats_report.py` pass unmodified. That is the regression proof for
Unit 1: both endpoints already have traversal coverage, so the extraction is
guarded by tests written against the old code.

Added on top, as direct unit tests of the helper, covering cases neither
endpoint tests today:

- an empty `report_path`
- a `./x`-style path
- a path resolving to the report root itself
- a symlink whose target escapes the root

The last is the case worth having: it is the one input where the prefilter does
not help, and both current call sites happen to reject it only because
`.is_file()` follows the link to a path outside the root. Pinning it makes that
explicit rather than incidental.

Frontend changes have no automated coverage by design — this repo has no
component-testing setup. Verification is manual at the running app, checking
that all three views still compute, recompute, and render their summary
numbers.

## Sequencing

Three commits, kept separable:

1. `refactor(api)` — extract `resolve_report_file`, point both report endpoints
   at it, add the helper's unit tests.
2. `refactor(ui)` — extract `useComputeResults`, adopt in the three views.
3. `tweak(ui)` — consolidate `Stat` onto one treatment.

Commit 3 carries the only visible change, isolated so it can be reverted
without touching the extraction.

## Requirements

- **R1.** `resolve_report_file` rejects any `report_path` containing a `..`
  segment, an empty segment, or an absolute path, without touching the
  filesystem.
- **R2.** `resolve_report_file` rejects any path whose resolved target is not
  contained within the supplied root, including via symlink.
- **R3.** `resolve_report_file` rejects a path whose resolved target is not a
  regular file, including the root directory itself.
- **R4.** `get_bam_stats_report` and `get_vcf_stats_report` return the same
  status code and body for every input they return today.
- **R5.** A user computing results from the BAM, VCF, or annotation Results view
  sees the same job queued, and the same "Computing results" notification, as
  before the refactor.
- **R6.** Summary statistics in `BamResults` and `VariantResults` render with a
  single consistent visual treatment.

## Decisions

| Decision | Rationale |
|---|---|
| Extract the containment guard despite no live bug | Two hand-rolled copies of a security check, with `.is_file()` load-bearing by accident, is a hole waiting for a third copy. |
| Take `is_relative_to` as the surviving spelling | Stricter of the two; does not depend on the `.is_file()` conjunction for correctness. |
| Do not extract a shared result table | ~30 shared lines against ~1,000 format-specific ones; named as a non-goal in #299. |
| Do not extract the index-path preamble | Five call sites for ~6 lines each; the repetition is in docstrings, not logic. |
| Take `VariantResults`' `Stat` styling | Newer of the two treatments; reads as the intended direction. |
| Leave `ExpressionResults` alone | Renders a facts table, not stat tiles. Converting is redesign, not refactor. |
