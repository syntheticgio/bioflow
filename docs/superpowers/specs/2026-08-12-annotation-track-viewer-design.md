# Annotation track viewer: features along a coordinate axis

Design for [#295](https://github.com/syntheticgio/bioflow/issues/295), the
follow-up to [#257](https://github.com/syntheticgio/bioflow/issues/257) where
track rendering was explicitly out of scope.

#257 shipped a feature summary, charts, and a searchable feature table. What
it cannot show is *position*: whether two genes overlap, which strand they sit
on, how exons are arranged inside a transcript, what the neighbours are. This
spec adds a coordinate-based track section to the existing annotation Results
view.

**The data this needs already exists.** #257 built a SQLite index
(`backend/app/pipelines/annotation_db.py`) holding every feature row -- parents
and children -- indexed on `(contig, start)`, `parent`, `type` and `name`. Its
`_where()` already uses overlap semantics (`start <= max AND end >= min`),
which is exactly the predicate a viewport needs. No reparse, no new artifact,
no new stored fact.

## What is actually new

Three things, and it is worth being precise because the issue reads bigger
than the work is:

1. A **window route** that returns features overlapping a region, with gene
   models assembled (parents plus their children), or binned counts when the
   region is too dense to draw individually.
2. A **reference resolution rule** with a refusal state, because the reference
   supplies the axis.
3. A **track section** in `AnnotationResults`, below the charts and above the
   feature table.

## Aggregation: fixed bin count, computed per viewport

At full-contig zoom a human chromosome holds tens of thousands of features
against ~1200 pixels. Returning them all is an unbounded payload; drawing them
all is a solid bar.

**The response switches shape by density, not the client.** One route, two
possible bodies, chosen server-side by a count:

- Fewer than **500** top-level features in the window: real features, with
  gene models.
- 500 or more: per-bin counts, drawn as a density band.

The threshold is a count of *top-level* features, taken with the same
`COUNT(*)` the route already needs, before any rows are fetched. 500 is
roughly where features stop being individually clickable at a typical section
width (~1200 px, so ~2 px per feature) -- below that the drawing is still
readable, above it a density band carries more information than a solid bar
of overlapping boxes.

It is one number in one place, and it is a rendering-legibility judgement
rather than a performance limit, so it is expected to be tuned once seen
against a real GFF3 rather than derived.

### Why not the Circos windowing scheme as-is

[`2026-08-10-circos-gc-tracks-design.md`](2026-08-10-circos-gc-tracks-design.md)
fixes 500 windows per contig, stored as a Mongo fact. Both properties are
wrong here, and the reasons are specific rather than stylistic:

- Its 500 is justified *radially* -- "roughly the angular resolution of a
  600 px-diameter ring." A linear viewer's resolution is its pixel width,
  which is not a constant.
- It bins **whole contigs**. A track viewer's viewport is a moving window
  *within* a contig. Zoomed into 50 kb of chr1, a contig-wide bin covers
  ~500 kb -- the aggregation has the wrong resolution everywhere except fully
  zoomed out.
- It is sized against the 16 MB document cap because it is *stored*. This is
  computed on demand and stored nowhere.

**What is reused is the principle, which does generalize:** a fixed bin
*count* rather than a fixed base width (bounded by construction -- the actual
insight), a minimum bin floor, `null` distinct from `0`, and parallel arrays
of rounded numbers rather than lists of objects.

### The binning query

Bins are computed in SQL, riding `ix_features_locus`:

```sql
SELECT (start - :win_start) / :bin_bases AS bin, COUNT(*)
FROM features
WHERE contig = :contig AND start <= :win_end AND end >= :win_start
GROUP BY bin
```

Measured on a synthetic 200,000-feature contig, a full-contig 600-bin query
runs in **~82 ms**. That is the worst case -- a full-contig scan. Zoomed-in
viewports restrict on `start` and are substantially cheaper.

`bin_bases = max(1, (win_end - win_start) / bins)`, with `bins` supplied by the
client from its pixel width, defaulting to **600**, and **clamped server-side
to the range 10-1000**. A client asking for a million bins must not be able to
make the server materialize a million rows.

Where a window is shorter than `bins` bases, `bin_bases` floors at 1 and the
response carries fewer bins than requested -- the array length is authoritative,
not the request.

**A bin with no features is absent from the result, and the client renders
absent as zero.** This is safe here in a way it is not for GC content: a
window with no annotated features genuinely *is* zero features, whereas a
window with no sequence is not 0% GC. The Circos spec's `null` ≠ `0` rule
exists for the latter case. Named explicitly so the divergence is a decision
rather than an oversight.

## Gene models: parents with their children

The value of a track over a table is seeing exon structure -- CDS blocks
joined by intron lines, the classic BED12 rendering. That needs parents and
children correlated in one response.

#257's table pages over top-level features and fetches children per-parent on
expand, because `LIMIT`/`OFFSET` must mean one thing. A viewport has no such
constraint: it is bounded by *coordinates*, so every row in the window can be
returned.

**Two queries, one response:**

1. Top-level features overlapping the window (`parent IS NULL`).
2. Their children, via `WHERE parent IN (...)` against `ix_features_parent`.

The cost is self-limiting: this path only runs below the density threshold, so
the parent set is already small by construction. No separate guard is needed.

### Orphaned children

A child whose parent falls outside the window -- or whose parent is missing
entirely, which happens in malformed files -- **is drawn, detached, on its own
row.** Silently dropping features from a view that claims to show a region is
the worse failure: the user sees empty sequence where a feature exists, with
nothing saying so.

### Row packing

Overlapping features on one strand cannot share a row. Rows are assigned
greedily by start coordinate, and **capped at 12 rows per strand**. Beyond the
cap the track renders `+N more — zoom in` rather than growing without bound;
an unbounded section pushes the feature table off the page at a dense locus.

## Reference resolution: the axis must not lie

**This is the part with a real correctness trap.** For #257 a wrong reference
gave wrong coverage *denominators* -- a percentage slightly off in a chart. For
a track viewer the reference supplies the **coordinate axis**. Get it wrong
and the viewer draws a ruler of the wrong length with features positioned
against it: a picture that looks authoritative and is silently false. A feature
at 4.2 Mb on a contig believed to be 3 Mb long is clipped or rescaled, and
nothing on screen says so.

Two tiers, then refusal:

**Tier 1 -- explicit provenance.** A FASTA in `derived_from`, **preferring
`ObjectRole.REFERENCE`** over bare FASTA format.

This requires fixing `_reference_for_annotation` (`pipeline_service.py:2371`),
which today returns the *first* FASTA parent with no role check. Its sibling
`reference_for_bam` twenty lines below already has the correct posture --
prefer the reference role, fall back to bare FASTA -- and this should match it.

That fix also corrects #257's coverage denominators for any annotation with
multiple FASTA parents, so it lands as a separate `fix(pipelines):` commit
ahead of the feature work.

**Tier 2 -- matching NCBI assembly accession.** A READY object in the same
project whose `ncbi_assembly_accession` fact equals the annotation's.

This fact is written by `ncbi_assembly.py:124` from an NCBI lookup response,
and lands on both genome and annotation automatically when a GFF3 is
downloaded alongside its assembly -- the usual way annotations arrive here. It
is *not* the hand-typed `assembly_accession` metadata field, which does not
exist for annotation formats at all (`INTERVAL_FIELDS` has no such field, and
`ASSEMBLY_ELIGIBLE_FORMATS = {FormatKind.FASTA}` means enrichment never infers
one for a GFF3).

`resolve_annotation_inputs` (`pipeline_service.py:2869-2895`) already does this
match for the consequence-annotation card, with the same reasoning, and this
should reuse that pattern rather than invent a second one.

Matching rules:

- **Exact string equality.** `GCF_000001405.39` and `GCF_000001405.40` are
  different assemblies with different coordinates and must not match.
- **GCA and GCF counterparts do not match**, `paired_accession`
  notwithstanding. Equating them is a coordinate claim this does not make.

**Tier 3 -- refuse.** The track section renders a plain state naming what
would fix it ("this annotation has no reference in its provenance, and no
project reference records a matching assembly accession"), not a bare
decline. The #257 charts and feature table remain fully available.

### Rejected: synthesizing an axis from the annotation

Deriving contig lengths from `MAX(end)` per contig always renders and never
refuses, which is why it is tempting. It is wrong: the axis then means "as far
as the last annotated feature reaches," not the contig length. A chr1 whose
last gene ends at 248 Mb draws as though the chromosome ends there, and
unannotated tail sequence can never be shown. An axis that quietly means
something other than it appears to is precisely the failure the Circos spec's
`null` ≠ `0` rule guards against.

### Rejected: a manual reference picker

Offering a dropdown when resolution fails hands the user a way to construct a
wrong axis by hand, with no check that would catch it.

### Contigs beyond the stored cap

`sequence_lengths` is capped at the **50 longest contigs**
(`MAX_STORED_CONTIGS`, `parsers.py:29`). A feature on contig 51 is in the
index but its contig has no recorded length, so no axis can be drawn for it.

The contig selector lists **only contigs with a known length**, and when the
annotation references contigs beyond that set it says so explicitly
(`N contigs not shown — no recorded length`). Listing a contig that cannot be
drawn, or omitting it silently, are both worse than saying which is the case.

## API

One route, alongside the existing `/annotationstats/*` routes in
`backend/app/api/v1/pipelines.py`:

```
GET /pipelines/annotationstats/window/{object_id}
    ?contig=chr1&start=1240000&end=1310000&bins=600
    [&feature_type=&biotype=&strand=]
```

Ownership is checked via `object_service.get_object` **before** the path is
built, for the same reason the sibling routes document: `annotation_stats_dir`
is laid out by object id alone, so that lookup is the only thing separating
one profile's annotations from another's.

Response, dense case:

```json
{ "mode": "binned", "bin_bases": 116667, "counts": [12, 7, 0, 31, ...] }
```

Response, feature case:

```json
{ "mode": "features",
  "features": [
    { "start": 1243000, "end": 1247000, "strand": "+", "type": "gene",
      "name": "SAMD11", "feature_id": "gene:1",
      "children": [ { "start": 1243000, "end": 1243400, "type": "exon" } ] }
  ],
  "truncated_rows": 0 }
```

The two shapes are distinguished by `mode` rather than by which key is present,
so a client cannot mistake an empty region for a dense one.

**Both shapes echo back `contig`, `start` and `end`.** Panning and zooming
issue overlapping requests that can return out of order, and a response
carrying no record of what it answers cannot be matched to the current
viewport -- the client would draw stale features at the wrong coordinates.

## Frontend

A `AnnotationTrack` section inside `AnnotationResults.tsx`, between the charts
and the feature table -- matching how every other results view in this repo is
built (one page, sections stacked) rather than introducing a tab pattern that
exists nowhere else.

Rendered as **inline SVG**, consistent with `CircosPlot.tsx`. No charting or
genome-browser library is added.

Controls: a contig selector, a `chr:start-end` locus box, and zoom in/out.
Feature-type filter chips are driven by the type counts #257 already computes,
so the chip list is the file's actual types rather than a hardcoded vocabulary.

**The track and the table cross-drive each other**, which is the main payoff of
keeping them adjacent: clicking a table row scrolls the track to that locus,
and clicking a feature filters the table to it.

Hovering a feature shows its details (name, type, coordinates, strand,
biotype); clicking opens the same detail the table row shows, rather than a
second presentation of the same record.

## Testing

Backend, `pytest`, run per CLAUDE.md via `./backend/run-worktree-tests.sh`
from this worktree:

- The binning query: bin boundaries, features straddling a bin edge, a bin
  with no features absent from the response, `bins` clamped at 1000.
- Mode selection: the same window returns `binned` above the threshold and
  `features` below it.
- Gene models: children attached to the right parent; an orphaned child
  returned detached rather than dropped.
- Row packing: overlapping features get distinct rows; the cap reports
  `truncated_rows` rather than growing.
- Resolution: role preference in tier 1; accession equality in tier 2; version
  suffixes and GCA/GCF counterparts **not** matching; refusal when neither
  tier fires.
- Ownership: a cross-profile request 404s, matching the sibling routes' tests.

**Check the resolution rule against the real database, not only fixtures.**
CLAUDE.md records that #257's own suggestion rules passed a green suite while
miscounting `protein.faa` and `cds_from_genomic.fna` as alignable references,
because the tests fed hand-built objects already shaped the way the rules
expected. Resolution here has the same hazard: a project with an assembly, its
`protein.faa`, and a GFF3 is exactly the shape that exposes a too-permissive
tier 1. Verify with `docker compose exec api python -c "..."` against real
objects.

Frontend verification is manual at `localhost:5273` via
`./ops/worktree-up.sh`, per this repo's convention -- there is no component
test setup and none is expected. Bring the stack down with
`./ops/worktree-up.sh --down` when finished.

Per CLAUDE.md, check `backend/app/services/suggestion_service.py` for any card
whose `unavailable` reason this work makes untrue.

## Out of scope

- **New annotation formats.** The supported set is #257's.
- **Editing feature records.** Read-only, like every other results view.
- **Sequence display.** No base-level rendering at maximum zoom; there is no
  reference sequence read path here, and adding one is a separate concern.
- **Multiple annotations on one axis.** One file, one viewer. Comparing two
  annotations against a shared reference is a different feature.
- **A gene-density Circos ring**, which is
  [#179](https://github.com/syntheticgio/bioflow/issues/179) and is a radial
  track, not this.
