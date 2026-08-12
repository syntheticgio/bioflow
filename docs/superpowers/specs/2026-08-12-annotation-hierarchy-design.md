# Annotation gene and transcript hierarchies

Design for [#296](https://github.com/syntheticgio/bioflow/issues/296).

[#257](https://github.com/syntheticgio/bioflow/issues/257) shipped a feature
table that presents every GFF/GTF/BED record independently, with a one-level
parent/child expansion bolted on. This design makes that hierarchy
*trustworthy* — every record accounted for, every broken link visible and
counted — and then builds a gene-first view on top of it.

## What exists today

Contrary to #296's framing, grouping is not absent. #257 shipped three
pieces of it:

- `annotation_parse.py` derives a `parent` string per row. GFF3 takes
  `Parent`; GTF infers from `gene_id`/`transcript_id`; BED is always `None`.
- `annotation_db.py` stores every row, indexes `parent`, pages over
  `parent IS NULL`, and serves `children_of()` unpaged per parent
  (`backend/app/pipelines/annotation_db.py:217`).
- `AnnotationFeatureTable.tsx` renders expand chevrons and recurses into
  child rows (`frontend/src/components/AnnotationFeatureTable.tsx:415`).

What is absent is any notion of whether a `parent` string *means* anything.
It is written at parse time and never resolved against the file's ID set.

### The bug this fixes

**A record whose `Parent` names a nonexistent feature is invisible in every
view.** It is excluded from the parent page because `parent IS NULL` is
false, and it appears under no expanded row because no row carries that
`feature_id`. Nothing counts it, nothing warns, and the feature total in the
summary does not reconcile with anything reachable in the table.

This is live in `main` today and is precisely what #296's design constraint
forbids: the file degrades to *hidden* records rather than visible ungrouped
ones. It is fixed here rather than tracked separately, because the mechanism
that fixes it — resolution status — is the same mechanism the rest of this
design is built from.

Four related silent failures share the cause:

- **Multi-parent truncation.** `parent.split(",")[0]`
  (`annotation_parse.py:127`) keeps the first parent of a GFF3 exon shared
  between two transcripts. The second transcript never shows that exon.
- **Unbounded recursion.** `FeatureRow` recurses into children with no depth
  guard. A self-referential or cyclic `Parent` chain hangs the browser.
- **Ambiguous IDs.** Duplicate `ID` values make `children_of(parent_id)`
  attach the same children to every row sharing that ID.
- **No integrity reporting.** `annotation_top_level_count` exists; nothing
  counts dangling, cyclic, or duplicated relationships.

## Decisions

### Unresolved records get their own view, not the parent page

A record with a broken parent link is preserved and reachable, but it does
**not** join the default paged list.

The alternative — promoting unresolved rows to the parent page with a
marker — keeps everything in one place, and that is its problem. On a badly
broken file the parent page fills with orphaned exons, burying the genes,
and the count being paged over stops meaning "top-level features". The
paging contract #257 was careful to protect is the first casualty.

Instead the table gains a three-position view toggle — **Genes**, **All
records**, **Unresolved** — and the unresolved count is surfaced as a
summary statistic with a one-click path into it. This follows a decision
this codebase already made: #257's type filter clears `top_level_only`
because "the default view is one slice and filters reach the rest" is how
this table works. Unresolved records are a fourth slice.

The issue's constraint is that malformed hierarchy degrades to *visible*
ungrouped records. A badged, counted, one-click-reachable view satisfies
that. It does not require the records to be in the default page.

### Resolution happens in SQL after the insert, not in a second file pass

Detecting dangling references, duplicate IDs, and cycles requires seeing the
whole file's ID set, which #257's single streaming pass does not.

The rejected alternative is a two-pass parse: collect every `feature_id` into
a Python set, then re-read the file resolving against it. That set is
O(distinct IDs) in RSS — roughly 3M short strings for a human GFF3, a few
hundred MB — held at exactly the moment the worker is also carrying a
10,000-row insert batch. This repo has been bitten by memory ceilings
repeatedly and runs under cgroup hard limits; a resolution strategy whose
peak scales with annotation size is the wrong shape here.

So: stream and insert exactly as today, then resolve with indexed `UPDATE`
statements against the built table. SQLite holds the ID set on disk. Memory
stays flat regardless of file size.

The accepted cost is that integrity *facts* come from queries after the
fact rather than from `AnnotationAccumulator`, which is a second source of
truth for the same numbers. This is contained by having one function compute
them once at job end, feeding both the stored facts and the table's own
counts — the table and the summary cannot disagree because they are the same
query.

A third option, resolving lazily at read time with a join per page, was
ruled out: it costs a join over millions of rows on every request and cannot
produce a file-level integrity count without a full scan.

### Cycles are caught by a depth cap, not by true cycle detection

The bounded walk that assigns each feature its depth stops at **100**.
Anything at depth 100 is marked `cyclic` and its subtree walk terminates.

A recursive CTE would detect genuine cycles exactly. A fixed cap is
preferred because the walk must not hang on a pathological or hostile file,
and because annotation hierarchies deeper than a handful of levels do not
occur in real data — gene → transcript → exon is three. A depth-100 chain is
already a broken file whether or not it technically closes into a cycle, and
both cases want the same treatment.

The same constant bounds the frontend's recursion, which is what fixes the
unbounded-render failure above.

### A gene is type-driven, with a stated structural fallback

The Genes view prefers rows whose `type` is in a gene synonym set (`gene`,
`pseudogene`, `ncRNA_gene`). When a file contains none, it falls back to
resolved roots and **says so in the UI**.

Type-driven alone leaves an empty view on the flat Bakta GFF3s and BED files
that #257 brought into scope. Structural alone — "resolved roots, relabeled
Genes" — is worse: NCBI GFF3s open with one `region` record per contig, so a
purely structural gene view lists contigs under a heading that says genes.
That is a view which looks right and is not, which is the failure class this
issue exists to prevent.

The mode is decided by the data, not the frontend: a `COUNT(*)` over the
synonym set runs once at job time and is stored as a fact.

### Gene summaries are stored, not computed on expand

A `genes` table is built at job time, one row per gene, carrying transcript
count, descendant count, span, strand, and biotype.

Computing these on demand means walking two levels down for every one of 25
visible rows on every page turn — the shape #257 avoided when it made
`has_children` a single indexed `EXISTS`, and it degrades exactly on the
large files where the view earns its place.

Storing them is the trade `annotation_stats.py` already makes: bounded work
once at compute time, cheap reads forever. The table is O(genes), not
O(features). It also gives the integrity walk a second payoff — the same
depth-capped traversal that detects cycles computes subtree counts, so it is
one traversal producing both.

## Requirements

Identifiers are permanent and are not reused, per `CLAUDE.md`.

### Hierarchy resolution

- **AH-1** — The compute job assigns every stored feature a `parent_status`
  of exactly one of `root`, `resolved`, `dangling`, `ambiguous`, `self`, or
  `cyclic`.
- **AH-2** — A feature with no parent reference is assigned `root`.
- **AH-3** — A feature whose parent reference matches exactly one distinct
  `feature_id` in the same file is assigned `resolved`.
- **AH-4** — A feature whose parent reference matches no `feature_id` in the
  same file is assigned `dangling`.
- **AH-5** — A feature whose parent reference matches more than one row
  carrying that `feature_id` is assigned `ambiguous`.
- **AH-6** — A feature whose parent reference equals its own `feature_id` is
  assigned `self`.
- **AH-7** — A feature whose ancestor walk reaches depth 100 without
  terminating is assigned `cyclic`.
- **AH-8** — The compute job assigns every feature whose `parent_status` is
  `root` or `resolved` a `depth`, where a `root` feature has depth 0 and a
  `resolved` feature has one more than its parent.
- **AH-9** — A feature whose `parent_status` is `dangling`, `ambiguous`,
  `self`, or `cyclic` is stored with `depth` 100, which is the cap and is
  not a position in a tree.
- **AH-10** — No feature present in the source file is absent from the
  stored table, whatever its `parent_status`.

### Multi-parent records

- **AH-11** — A GFF3 feature declaring multiple parents is stored once per
  declared parent relationship, so that expanding any one of those parents
  shows the feature.
- **AH-12** — A feature stored under multiple parents reports the same
  feature counts to the summary as it would under one, so that the feature
  total is not inflated by relationship multiplicity.
- **AH-36** — A feature reachable from one gene by more than one path
  contributes exactly one to that gene's descendant count.

### Integrity reporting

- **AH-13** — The compute job stores a count of features per `parent_status`
  value as object facts.
- **AH-14** — The compute job stores the maximum observed `depth` as an
  object fact.
- **AH-15** — The Results view displays the total count of features whose
  `parent_status` is not `root` or `resolved`.
- **AH-16** — A user can reach the Unresolved view in one interaction from
  the displayed unresolved count.
- **AH-17** — The Results view displays no integrity block when every
  feature is `root` or `resolved`.

### Gene-first navigation

- **AH-18** — The compute job stores one `genes` row per feature whose
  `type` is in the gene synonym set.
- **AH-19** — When a file contains no feature typed in the gene synonym set,
  the compute job stores one `genes` row per feature whose `parent_status`
  is `root`.
- **AH-20** — The compute job stores which of the two rules in AH-18 and
  AH-19 produced the `genes` table as an object fact.
- **AH-21** — The Genes view states that it is showing top-level features
  when the fallback rule of AH-19 produced the table.
- **AH-22** — Each `genes` row carries the count of its direct children.
- **AH-23** — Each `genes` row carries the count of all its descendants to
  the depth cap.
- **AH-24** — Each `genes` row carries the minimum start and maximum end of
  its own interval and all its descendants' intervals.
- **AH-25** — A user can expand a gene row to its direct children, and any
  child row to its own children, to the depth cap.

### View toggle

- **AH-26** — The feature table offers exactly three views: Genes, All
  records, and Unresolved.
- **AH-27** — The All records view applies no `parent_status` filter, so
  that it returns every stored feature subject only to the user's own
  filters.
- **AH-28** — The Unresolved view returns exactly those features whose
  `parent_status` is `dangling`, `ambiguous`, `self`, or `cyclic`.
- **AH-29** — Each row in the Unresolved view displays its `parent_status`
  and its unresolved parent reference.
- **AH-30** — Changing the view resets pagination to the first page.
- **AH-31** — Changing the view discards expansion state, so that no
  expanded child row from a previous view survives into the new one.
- **AH-32** — The frontend renders no more than 100 levels of nested child
  rows.

### Non-functional

- **AH-33** — Peak additional resident memory during resolution does not
  scale with the number of features in the annotation.
- **AH-34** — A page of the Genes view returns without querying the
  `features` table for per-gene counts.
- **AH-35** — Resolution and gene-table construction complete within the
  existing annotation compute job, with no new job type.

## Components

### `annotation_db.py` — resolution and the genes table

Two new columns on `features`: `parent_status TEXT NOT NULL` and
`depth INTEGER NOT NULL`. One new index, `ix_features_feature_id`, without
which every resolution `UPDATE` is a scan.

New functions, all called once by the compute job after
`build_annotation_db` has inserted and indexed:

```python
def resolve_hierarchy(*, db_path: Path) -> dict
def build_gene_table(*, db_path: Path) -> dict
```

`resolve_hierarchy` runs the status `UPDATE`s in dependency order — `root`,
then `self`, then `ambiguous` against a duplicate-ID subquery, then
`dangling` against a `NOT EXISTS`, then `resolved` for the remainder — and
then performs the depth-capped walk that assigns `depth` and reclassifies
depth-100 rows as `cyclic`. It returns the per-status counts, so the facts
and the table are the same query.

`build_gene_table` creates and fills `genes`, choosing its rule by the
synonym-set count, and returns the mode alongside the row count. The
descendant counts and spans come from the same walk, accumulated upward.

`FeatureFilters` gains `parent_status: tuple[str, ...] | None`, which is how
the Unresolved view expresses itself. `top_level_only` stays as-is; the
Genes view does not use `features` for its page at all.

### `annotation_parse.py` — multi-parent preservation

`Feature.parent` becomes `Feature.parents: tuple[str, ...]`, and
`build_annotation_db` writes one row per parent. A feature with no parent
writes one row with an empty tuple.

This is the one change that touches row counts, and AH-12 is what keeps it
honest: the accumulator counts source lines, not stored rows, so the feature
total in the summary continues to mean what it meant.

### `pipelines.py` — one new route parameter, one new route

The features route gains `view: Literal["all", "unresolved"]`, which maps to
`parent_status` filters. Genes get their own route rather than a third enum
value, because they page over a different table with a different row shape:

```
GET /annotationstats/genes/{object_id}
```

The children route gains a `depth` parameter so the client cannot request
beyond the cap.

### `AnnotationFeatureTable.tsx` — the toggle and bounded recursion

The three-position toggle sits above the existing filter row. `FeatureRow`
takes a `depth` prop and refuses to recurse past the cap. Gene rows render
their stored summary; unresolved rows render their status and dangling
reference.

The existing effect that clears expansion state on filter change
(`AnnotationFeatureTable.tsx:91`) extends to cover view change, which is
AH-31.

### `AnnotationResults.tsx` — the integrity line

One block, rendered only when the unresolved count is non-zero, stating the
count and linking into the Unresolved view. Absent rather than empty on a
clean file, following the pattern `annotation_stats.finish()` already uses
for its optional fact keys.

## Testing

Parser and resolution tests are plain function calls over hand-written
fixture lines, following #257's testing decision.

- Resolution status assignment, one test per `parent_status` value, over a
  fixture file constructed to contain each case.
- The reconciliation property of AH-10: every source line appears in the
  stored table.
- Multi-parent storage: a GFF3 exon with `Parent=a,b` appears under both
  `a` and `b`, and the summary feature count is unchanged (AH-11, AH-12).
- Depth-cap termination on a fixture with a two-node cycle and on a fixture
  with a self-referential row.
- Gene-table mode selection: a typed fixture picks AH-18, a Bakta-shaped
  flat fixture picks AH-19, and the stored mode fact matches.
- Descendant counts and spans against a three-level fixture whose expected
  values are computed by hand.
- The de-duplication property of AH-36: a fixture where one exon is shared
  by two transcripts of the *same* gene counts that exon once in the gene's
  descendant total.
- The empty-integrity-block case: a clean file stores no unresolved counts
  and renders no block.

Per `CLAUDE.md`'s note on checking rules against the real database, the
resolution pass is additionally exercised against a real NCBI GFF3 through
`docker compose exec api python -c ...` — the `region`-record-per-contig
shape that motivates AH-19's fallback is exactly the kind of thing a
hand-built fixture is constructed not to have.

## Out of scope

- Cross-file hierarchy. Relationships are resolved within one annotation
  file only.
- Repairing broken hierarchy. Unresolved records are reported, never
  reparented — this is the issue's design constraint restated.
- True cycle detection beyond the depth cap.
- Gene-level aggregate charts. The gene table exists for navigation; the
  Results view's existing charts are unchanged.
