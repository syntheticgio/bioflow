# Circos plot: GC content and GC skew

Design for [#151](https://github.com/syntheticgio/bioflow/issues/151), the
fifth of the five reference visualizations tracked by
[#146](https://github.com/syntheticgio/bioflow/issues/146).

The epic's design pass
([`2026-08-10-reference-visualizations-design.md`](2026-08-10-reference-visualizations-design.md))
scoped this to **GC content and GC skew only**, splitting repeat density into
[#177](https://github.com/syntheticgio/bioflow/issues/177) and gene density
into [#179](https://github.com/syntheticgio/bioflow/issues/179) because both
need heavyweight annotation tooling that does not exist. That scoping stands.

This spec answers the three questions it left open — window size, where the
per-window data lives, and the rendering approach — and corrects one premise
of the design pass that turns out to be wrong in a way that changes the
architecture.

## The correction: this cannot ride the parser

The design pass called the GC scanner "a self-contained scanner over the
FASTA … a modest amount of new code," implying it could sit alongside the
existing composition statistics. It cannot, and the reason is specific.

`sequence_stats.fasta_stats` (`backend/app/storage/sequence_stats.py:195`)
already computes whole-assembly GC, and it is **a sampler, not a scan**. It
takes a `max_bases: int = 50_000_000` budget and, for uncompressed files
larger than that, spends the budget in equally spaced blocks across the file
(`_fasta_sample_strided`, `:286`) rather than reading it through. Its own
docstring is explicit that the cap "is a performance guard" and that a
prefix read "describes chr1, not the assembly."

Sampling is correct for a single aggregate GC figure. It is **structurally
incapable** of producing the tracks this feature needs:

- A strided sample reads ~2,000 disjoint blocks. A window track needs
  *contiguous, ordered* windows across every contig. The sampler's own seek
  logic discards a partial line after each seek (`:309-313`), so it does not
  even know where in a contig its bytes came from.
- **GC skew is cumulative and order-dependent.** `(G−C)/(G+C)` per window,
  read as a running trend around the chromosome, is what locates the origin of
  replication. A sampled subset of windows in unknown positions has no trend
  to read.

So GC-by-window is **a job, not a parse-time fact**, and the fact document
gains its data from a handler rather than from `parsers.py`. This also
sidesteps the 256MB `FASTA_EXACT_LIMIT` truncation rule
(`parsers.py:40`, `:578`) — a job reads the whole file with a lease, which is
what `assess_completeness` and every other assembly-QC job already do.

Calling this out because the cheaper-looking alternative is genuinely
tempting and silently wrong: adding a few counters to `_parse_fasta`'s
existing loop would produce windows for small genomes, pass every test written
against small fixtures, and emit a biased or truncated track for exactly the
multi-chromosome genomes a Circos plot is for.

## Window size

**Fixed window count per contig, not a fixed base count.** 500 windows per
contig, floored at a 100 bp minimum window.

A fixed base width (say 10kb) is the obvious choice and is wrong here for the
same reason the raw contig list was wrong for #147: it is unbounded. A 3 Gb
genome at 10 kb windows is 300,000 windows against Mongo's 16 MB document cap,
while a 5 Mb bacterial genome is 500 — the small case gets too few points and
the large case blows the budget.

A fixed count is bounded by construction and renders identically at every
genome size, which is what a radial plot needs: every ring is drawn at the
same angular resolution regardless of the genome's length, so a fixed count
maps 1:1 onto the pixels available. 500 windows is roughly the angular
resolution of a 600 px-diameter ring — more points than that are sub-pixel and
cannot be seen.

The 100 bp floor stops a short contig from producing meaningless windows: a
2 kb plasmid divided 500 ways is 4 bp per window, where GC skew is noise. Below
the floor the contig gets `floor(length / 100)` windows instead.

**Only contigs above a length threshold get tracks at all.** A draft assembly
with 200,000 short contigs has no meaningful circular representation, and one
document cannot hold tracks for all of them. Store tracks for the **longest 50
contigs**, reusing the existing `MAX_STORED_CONTIGS = 50` (`parsers.py:29`)
rather than introducing a second number for the same idea, and set
**`gc_tracks_partial`** when contigs were omitted — matching the
`gfa_topology_partial` / `sequence_lengths_partial` convention already in the
tree.

## What gets stored

One fact, `gc_tracks`, on the assembly object:

```
{
  "window_count": 500,
  "contigs": [
    {
      "name": "chrI",
      "length": 230218,
      "window_bases": 460,
      "gc": [41.2, 39.8, ...],      # percent, one per window
      "skew": [-0.02, 0.01, ...]    # (G-C)/(G+C), one per window
    },
    ...
  ]
}
```

`gc` and `skew` are **parallel arrays of numbers**, not a list of window
objects — the same positional-array reasoning `sequence_nx_curve` and #149's
segment list use. 50 contigs × 500 windows × 2 tracks is 50,000 floats, which
is comfortably inside the document cap when stored as bare numbers and is not
when each carries key names.

Round to 2 decimal places on write. Full float precision triples the stored
size to encode noise well below what a ring can render.

**A window with no A/C/G/T (an all-N gap) stores `null`, not 0.** Zero GC and
"no sequence here" are different facts, and a gap plotted as 0% GC draws a
cliff that reads as a real compositional feature. The renderer breaks the line
at nulls.

## Backend

**New `backend/app/pipelines/gc_tracks.py`** — a pure function
`compute_gc_tracks(path, compression, *, cancel_event) -> dict`, following the
shape of `sequence_stats.fasta_stats` (same signature style, same cancel
handling, same "return {} on unreadable" contract) but scanning rather than
sampling.

It streams the FASTA once, accumulating per-contig base counts into windows
sized from that contig's length. Because window width depends on total contig
length and length is not known until the contig ends, either buffer the
contig's per-base counts at a fine fixed granularity and re-bin at commit, or
make two passes. **Re-bin from a fine granularity** — 10,000 fine bins per
contig, aggregated down to the final 500 — since a second pass over a
multi-GB file to learn lengths that `_parse_fasta` already computed is the
larger cost.

Cancellation must be checked on the same cadence as the existing scanners
(`sequence_stats.py:280`, every 100,000 lines) — this is the longest-running
pure-Python loop the codebase would have, and an uncancellable one blocks a
worker slot for the length of a genome.

**New handler `analyze_gc_tracks`** in
`backend/app/queue/assembly_qc_handlers.py`. This is pure Python with no
subprocess, so it is `HandlerMode.THREAD`, not `SUBPROCESS` —
and that carries a trap CLAUDE.md names explicitly: **a thread handler must
not call `asyncio.run()`** to reach any async helper. If this handler needs to
touch Mongo through an async path, use `app.db.client.run_from_thread`. A
second event loop makes Motor raise "attached to a different loop" the moment
a query runs, with every unit test still green because the tests mock the
seam.

`JobResources(cpu=1, mem_mb=2048, io=IoClass.HEAVY)` — one core (the loop is
single-threaded), modest memory (fine bins for one contig at a time), heavy IO
(it reads the entire file).

**New `launch_gc_tracks`** in `pipeline_service.py`, modelled on
`launch_completeness` (`pipeline_service.py:3633`) — the precedent for a
read-only, facts-only QC job. Single-input, unlike #149's two-object launcher:
it takes only the assembly. `dedup_key=f"gc_tracks:{obj.id}"`.

**No run record, and no new `RunJobRole`.** `launch_completeness` enqueues its
job directly with no `create_run` and no `link_job`, and this follows it. A
`Run` groups the jobs that together produced an artifact; this job produces no
object, only facts merged onto one that already exists — the same shape
`assess_completeness` describes as "read-only … no new object, only facts
merged onto the assembly" (`assembly_qc_handlers.py:405`). Adding a run for it
would put a one-job entry in the activity view for something the user
experiences as a property of the assembly, not as a pipeline run.

This is a real difference from #149, which *does* create a run: a synteny
comparison is a two-input operation against a named reference, and which
reference it ran against is exactly the kind of thing a run record exists to
remember.

## Frontend

**New `frontend/src/components/CircosPlot.tsx`**, hand-rolled SVG, **no
library**.

This does not repeat #150's cytoscape departure, and the distinction is worth
stating because the two look superficially similar. #150 needed a *computed
force-directed layout* — genuinely hard, iterative, and the reason a
dependency cleared the bar. A Circos plot has no layout problem at all:
every element's position is `(radius, angle)` where the angle is a known
fraction of a known total length. It is trigonometry over data whose positions
are fully determined, which is the case this repo's hand-rolled SVG charts
already cover.

Structure:

- **Outer ring**: contig arcs around the perimeter, sized proportionally to
  length, separated by a small angular gap. Labels on arcs wide enough to
  carry one.
- **Middle ring**: GC content, drawn as a filled area against the assembly's
  mean GC as baseline. Deviation from the mean is the signal — a horizontally
  banded genome is unremarkable, a region far off the mean is a candidate
  horizontal transfer or a contaminant contig.
- **Inner ring**: GC skew, drawn as a **diverging** fill — positive and
  negative in different colours against a zero baseline. Skew's whole
  diagnostic value is its sign flip, and a single-colour fill hides exactly
  that. The two points where a bacterial chromosome's skew changes sign are
  the origin and terminus of replication.

Per the `dataviz` conventions this repo follows for anything with a
sequential/diverging distinction: GC content is sequential (one hue, varying
lightness), GC skew is diverging (two hues meeting at zero). Both must hold up
in light and dark themes.

**Render only for assemblies with few enough contigs to be legible** —
say 24 or fewer tracked contigs. Above that the perimeter is a picket fence of
unlabelled slivers, which is not a smaller version of the visualization. Show
the plot for finished and near-finished genomes, and nothing for fragmented
drafts, matching how `<NxChart>` simply does not render NGx when genome size
is absent.

Wired into `AssemblyFacts.tsx` alongside `<NxChart>` and `<BuscoChart>`.

**Built to take more rings.** #177 and #179 each add one, and both are
specified against "whatever windowing scheme #151 establishes" — which this
spec fixes as 500-windows-per-contig with the parallel-array layout above.
Accept tracks as a list of ring descriptors rather than hard-coding two, so
adding a third is a data change rather than a component rewrite.

## Suggestion rule

A card in `suggestion_service.py` offering GC track analysis for
reference-role FASTA objects, plus a test case in
`test_suggestion_service.py`. The same two traps that apply to #149 apply
here: `protein.faa` is FASTA but has no meaningful GC skew, and duplicate
assemblies must be deduplicated by digest.

Additionally gate the card on contig count where it is already known —
offering a Circos plot for a 200,000-contig draft is offering a run whose
output will not render.

## Testing

`compute_gc_tracks` is pure and gets ordinary pytest coverage:

- a contig of known composition, asserting GC percent per window
- a contig with a known skew sign flip, asserting the sign changes at the
  right window — this is the feature's whole diagnostic and the one test that
  would catch a G/C transposition in the formula
- an all-N window, asserting `null` rather than `0`
- a contig shorter than 500 × 100 bp, asserting the floor reduces the window
  count rather than producing 4 bp windows
- lowercase sequence (soft-masked FASTA is common), asserting `g`/`c` count —
  the existing `fasta_stats` handles this at `sequence_stats.py:251` and a new
  scanner that forgets it silently halves GC on a masked genome
- more than 50 contigs, asserting the longest 50 are kept and
  `gc_tracks_partial` is set
- a cancel event set mid-scan, asserting `JobCancelled` propagates

Run from a worktree with `./backend/run-worktree-tests.sh tests/ -q`.

Frontend verification is manual at localhost:5273. **Verify against a real
bacterial genome**, where the skew sign flip is a known biological fact with a
known answer — a synthetic fixture confirms the code draws what it was given,
not that what it was given is right. E. coli or B. subtilis both have
well-documented origins.

## Sequencing

Independent of #149. This is the larger of the two: a new scanner, a new job,
and the most involved rendering component of the five visualizations.

#177 and #179 both depend on this landing first, since both add rings to the
component and windows to the scheme this spec defines.
