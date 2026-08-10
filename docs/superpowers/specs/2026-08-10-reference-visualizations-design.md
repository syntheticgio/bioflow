# Additional visualizations for references

Design for [#146](https://github.com/syntheticgio/bioflow/issues/146), which
tracks five reference/assembly-quality visualizations as separate subtasks.

## Scope

Two of the five are buildable now and are specified here in full:

- **[#147](https://github.com/syntheticgio/bioflow/issues/147)** — Nx/NGx contiguity curve
- **[#150](https://github.com/syntheticgio/bioflow/issues/150)** — assembly graph (GFA) viewer

Two are greenfield pipeline work whose real risk is tool selection, not
implementation detail. They get a decision record here — the choice that
unblocks them — and their own spec later:

- **[#149](https://github.com/syntheticgio/bioflow/issues/149)** — synteny dot plot
- **[#151](https://github.com/syntheticgio/bioflow/issues/151)** — Circos plot

**[#148](https://github.com/syntheticgio/bioflow/issues/148) is already done**
and is closed. `76e34e2 feat(frontend): add BUSCO completeness stacked bar
chart` shipped it as `frontend/src/components/BuscoChart.tsx`. The epic body
still lists it as open; that listing is stale.

The split is a cost decision. #147 and #150 are "the data already exists, or
nearly does — persist it and draw it." #149 and #151 each need a new
whole-genome analysis capability, and picking the tool retires most of their
risk without writing an implementation plan for either.

## #147 — Nx/NGx contiguity curve

An Nx curve plots contig length (Y, log scale) against percentage of the
assembly (X). A curve that stays high before dropping sharply is a contiguous
assembly; one that drops immediately is mostly small fragments. It is the
continuous form of the N50 the tab already reports — N50 is the single point
at x=50, and the curve is why two assemblies can share an N50 and still be
very different.

### Backend: persist a downsampled curve

`_contiguity_stats` (`backend/app/storage/parsers.py:427`) already receives
`all_lengths` — every record's true length, uncapped, deliberately not the
`MAX_STORED_CONTIGS`-capped `lengths` dict. It computes N50/N90/L50/auN from
that list and then discards it.

It gains one more output: **`sequence_nx_curve`**, an array of 100
`[percent, length]` pairs for x = 1..100, where the value at x is the length
of the contig at which cumulative length first reaches x% of the total. This
is the same walk that already produces N50 and N90, generalized to a hundred
thresholds instead of two, and computed in one pass over the sorted list.

A hundred points rather than the raw list, because the raw list is unbounded:
a fragmented draft is hundreds of thousands of contigs, fact documents live in
Mongo, and Mongo caps a document at 16MB. A hundred points is a fixed cost for
an 8-contig finished genome and a 500,000-contig draft alike, and it is
exactly what the chart renders — no client-side reduction, no cap-versus-truth
ambiguity of the kind the `_contiguity_stats` docstring already warns about
for N50.

The tradeoff to record: the raw lengths remain unavailable afterwards, so a
future visualization wanting a different derived statistic needs a reparse
rather than a recomputation. That is accepted. Nothing currently wants one,
and reparsing is a background job, not a user-facing cost.

**The curve inherits the existing truncation rule and must not invent a second
one.** `parsers.py:558` emits contiguity facts only on the complete-parse
branch; a FASTA that hit the byte limit gets `sequence_lengths_partial` and no
N50 at all. The reasoning in that comment applies unchanged to the curve: a
curve computed from the first 256MB of a large draft is not an approximation
of the real curve, it is a different curve over a biased population (the file's
leading records, not its longest). `sequence_nx_curve` is therefore emitted
from inside `_contiguity_stats` and inherits the guard for free.

### NGx, and where genome size actually lives

NGx normalizes against *expected* genome size rather than the assembly's own
total, which is what makes two assemblies of the same organism comparable and
what reveals missing sequence.

The constraint that shapes the UI: **BioFlow has no genome-size estimate for
an arbitrary FASTA.** Genome size exists only as an assembly *parameter* —
`assembly_params.py:75`, surfaced into provenance as
`assembly_genome_size` with an `assembly_genome_size_source` of
`unset | user | inferred` (`queue/results.py:1316-1322`). An assembly BioFlow
produced from reads with a size supplied has one. An uploaded FASTA or an NCBI
download does not, and those are a large share of what reaches the Reference
Quality tab.

So NGx is derived on the frontend from the stored Nx curve plus that
provenance value, and the component degrades to a single line when it is
absent:

- **Genome size known** — Nx as a solid line, NGx as a dashed second line,
  legend shown.
- **Genome size unknown** — Nx alone. No empty state, no disabled control, no
  explanatory message. Nx is always computable, so there is nothing missing to
  report.

Both curves are drawn on one chart rather than behind a toggle, because the
gap between them is the diagnostic and a toggle hides it.

**NGx may legitimately end before x=100 and must not be clamped.** When the
assembly totals less than the expected genome size, the cumulative length never
reaches 100% of that size, and the curve stopping short is precisely the
"assembly is missing sequence" signal. Extending it to the axis would erase the
finding.

### Frontend

New `frontend/src/components/NxChart.tsx`, following `BuscoChart.tsx`:
hand-rolled SVG, an `aria-label` describing the curve, no dependency. Y axis is
**log scale** — on a linear axis every real assembly renders as a cliff against
the axis, which is unreadable.

Wired into the Reference Quality tab in `AssemblyFacts.tsx` (855 lines
already; the chart goes in its own file for the same reason `BuscoChart` did).

### Testing

`_contiguity_stats` is pure and already covered by pytest. New cases:

- a single contig (curve is flat at that length across all 100 points)
- uniform lengths (flat curve, N50 equals every point)
- one dominant contig plus a long tail (the characteristic sharp drop)
- an empty length list (returns `{}`, unchanged from today)
- a truncated parse emits no curve, asserted on the same branch that asserts
  no N50 today

Run from a worktree with `./backend/run-worktree-tests.sh tests/ -q`, never
`docker compose exec api` — that tests main's code, not the worktree's.

Frontend verification is manual in the browser, per this repo's convention:
`./ops/worktree-up.sh`, then the Reference Quality tab at localhost:5273.
Check both states — an assembly built by BioFlow with a genome size (two
curves) and an uploaded FASTA (one).

## #150 — Assembly graph (GFA) viewer

Nodes are sequences, edges are the connections between them. Tangles and
bubbles mark unresolved repeats, which is actionable in a way a contig count
is not: it tells the user the fix is long-read data, not another parameter
sweep.

### Two corrections to the issue's premises

The issue says the GFA is "ingested as an opaque blob" and that the work is
"parse + visualize." Both need adjusting, and each changes the task:

- **A GFA parser already exists.** `_parse_gfa`
  (`backend/app/storage/parsers.py:582`) already walks S and L lines, already
  handles the `LN:i:` tag and the `*` placeholder sequence, and already has a
  byte budget and a `gfa_counts_partial` flag. It keeps `gfa_segment_count`,
  `gfa_link_count`, `gfa_total_length` and discards the topology. The backend
  task is to **retain what is already being read**, not to write a parser.
- **The frontend does draw graphs, but never lays one out.**
  `WorkflowCanvas.tsx` renders an interactive node-and-edge SVG canvas in 1,090
  lines with no graph library. But its positions are user-placed and persisted
  (`NodePosition`). An assembly graph arrives with no positions. *Computing a
  layout* is the new capability, and it is the whole of the difficulty.

### Backend

`_parse_gfa` gains topology alongside the counts it already emits: segments as
`(id, length)`, links as `(from, to, from_orient, to_orient)`. Orientation is
kept because a GFA link carries strand and an assembly graph read without it
misrepresents which end of a segment joins which.

Topology is capped by segment count, with a **`gfa_topology_partial`** flag
when the graph exceeds the cap — mirroring the existing `gfa_counts_partial`
convention rather than introducing a second vocabulary for the same idea. The
counts remain exact in that case: they are cheap and already correct, and a
graph too large to draw is still a graph worth counting.

### Frontend: add cytoscape.js

Render with **cytoscape.js** using its `cose` force-directed layout. Node
radius scales with segment length, colour marks length tier; pan and zoom come
from the library.

Taken as a **normal npm dependency** in `package.json`, alongside the six
runtime dependencies already there. Vendoring into the tree was considered and
rejected: it would not change a single byte that reaches the browser (Vite
bundles from `vendor/` exactly as from `node_modules/`), and its main
attraction — escaping a transitive dependency tree — does not apply, because
cytoscape has none. What it would cost is a second dependency pattern
alongside the existing six and the loss of automated security advisories.

This is a **deliberate departure from a stated convention**, and the spec
records it as such rather than letting it look like an oversight.
`SequenceCharts.tsx` says, of its hand-rolled SVG, that "the smallest chart
dependency would outweigh the entire rest of the bundle."

Measured, as of cytoscape 3.34.0: **MIT licensed, zero runtime dependencies,
435KB minified, 137KB gzipped.** The gzipped figure is what crosses the wire,
and this is a localhost-served application — the bundle is read off a loopback
interface, not a mobile network. That is a real cost, weighed and accepted,
and much smaller than the convention's phrasing implies.

The reason to depart: the alternative was a hand-rolled Fruchterman–Reingold
layout capped at a couple of thousand segments, showing counts and a "too
large to draw" message above that. That is a feature that works on a finished
bacterial genome and fails on a fragmented eukaryotic draft — the case where
looking at the graph is most informative, because a hairball is the finding.
A cap that excludes the diagnostic case is not a smaller version of the
feature.

**`SequenceCharts.tsx`'s comment must be updated in the same commit.** Left
alone it becomes false the moment cytoscape lands, and this repo has already
been bitten by exactly that failure — `ToolMeta.runnable`'s comment cited
cutadapt and Trimmomatic as undispatched years after `trim_reads` grew its
three-way dispatch, and nothing caught it because a comment cannot fail. The
replacement states what will then be true: hand-rolled SVG for fixed, simple
shapes; a library where a computed layout is required, cytoscape being the one
case that cleared that bar.

### Testing

GFA parsing is pure Python and testable: a small graph with a bubble, a graph
with `*` in place of sequence, a graph whose segment count exceeds the cap
(counts exact, `gfa_topology_partial` set, topology absent).

The rendering component cannot be tested automatically — there is no jsdom or
testing-library in this repo and zero `.test.tsx` files. Verify in the browser
against a real Flye graph, which means running an actual assembly rather than
a fixture.

## #149 — Synteny dot plot (decision record)

**Decision: minimap2 with an `asm5`/`asm10`/`asm20` preset, emitting PAF. No
new tool dependency.**

The issue asks for MUMmer/nucmer to be evaluated against minimap2 "before
adding a new dependency." That evaluation resolves on the facts already in the
tree:

- minimap2 is **already installed and probed** (`tools.py:325`,
  `ToolMeta` at `tools.py:1231`).
- It is **already used for genome-to-genome alignment** by
  `ragtag_runner.py`, which scaffolds a draft against a reference — the same
  operation a dot plot visualizes. RagTag only emits placement stats and an
  AGP, not coordinates, which is why the runner is new even though the tool is
  not.
- **PAF is already the right shape.** Each record carries query
  start/end, target start/end, and strand — a dot plot's input, with no
  format conversion.

nucmer resolves finer small-scale rearrangements than minimap2 at an asm
preset. That is real, and not worth a new dependency for a visual overview
whose purpose is spotting translocations, large inversions, and chromosome
jumps — all of which are visible at minimap2's resolution.

Preset selection follows divergence: `asm5` for near-identical genomes,
`asm20` for more distant ones. Whether the user picks or the runner infers is
left to the implementation spec.

**Open question that spec must answer:** coordinate volume. A large genome pair
produces very large PAF files, and the same bounded-persistence question #147
answered with a 100-point curve arises here in two dimensions. Binning or
minimum-alignment-length filtering is likely, but the shape should be chosen
against a real PAF, not guessed.

## #151 — Circos plot (decision record)

**Decision: scope to GC content and GC skew only. Repeat density and gene
density split into separate follow-up issues.**

The issue asks for four rings — GC content, gene density, repeats, GC skew.
Their costs are nothing alike:

- **GC content and GC skew by window** are a self-contained scanner over the
  FASTA. No new tool, no annotation, a modest amount of new code. `gc_content`
  today exists only as a *read-level* fastp statistic
  (`fastp_runner.py:385`), so this is new positional data — but it is
  computable from sequence alone.
- **Repeat density** needs repeat masking — RepeatMasker or similar, a new
  heavyweight dependency with its own library downloads.
- **Gene density** needs gene annotation, which this pipeline does not produce
  for arbitrary assemblies. BUSCO's completeness check is not a substitute: it
  reports which lineage genes are present, not where anything sits on a contig.

The reduced scope still delivers the diagnostic the issue itself calls out:
**GC skew visually pinpoints a bacterial chromosome's origin of replication**,
and it needs nothing but the sequence. A two-ring Circos that ships is worth
more than a four-ring Circos blocked behind two tool installations.

Each deferred ring becomes its own issue, gated on the annotation tooling it
needs, and can be added to the same rendering component later — a Circos plot
is built to take more rings.

## Sequencing

#147 and #150 are independent and touch different parsers, different facts,
and different components. Either can go first; #147 is the smaller and lands
sooner.

#149 and #151 need their own specs before any implementation plan.
