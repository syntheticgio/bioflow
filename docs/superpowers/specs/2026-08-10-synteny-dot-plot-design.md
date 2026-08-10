# Synteny dot plot

Design for [#149](https://github.com/syntheticgio/bioflow/issues/149), the
fourth of the five reference visualizations tracked by
[#146](https://github.com/syntheticgio/bioflow/issues/146).

The epic's design pass
([`2026-08-10-reference-visualizations-design.md`](2026-08-10-reference-visualizations-design.md))
settled the tool question — **minimap2 emitting PAF, no new dependency** — and
left one open question for this spec: *coordinate volume*. That question turns
out to have a cleaner answer than the design pass anticipated, and it drives
most of what follows.

## What the plot is

Reference genome on X, the assembly's contigs on Y, each alignment drawn as a
short line segment from its start coordinate to its end. A correct assembly
forms a continuous diagonal. The three findings the plot exists to surface:

- **A break in the diagonal** — the assembly is missing sequence there, or the
  reference has sequence the assembly does not.
- **A segment on the opposite diagonal** (down-right rather than up-right) —
  an inversion. This is why strand must survive into the stored data.
- **A jump to a different Y band** — a translocation, or a contig that spans
  two reference chromosomes, which usually means a misjoin.

## Two things already in the tree that shape this

**`Divergence` already exists, and it is already a user-facing choice.**
`ragtag_runner.py:31` defines `SAME_SPECIES` / `SAME_GENUS` / `DISTANT`,
mapped at `ragtag_runner.py:38-40` to exactly `-x asm5` / `-x asm10` /
`-x asm20`. It already reaches the user through `launch_scaffold`'s
`divergence` parameter (`pipeline_service.py:4040`) and a dialog chooser.

So the design pass's open question — "whether the user picks or the runner
infers" — is already answered by precedent: **the user picks, from the enum
that already exists.** Do not add a second divergence vocabulary, and do not
infer. `_mm2_preset` (`ragtag_runner.py:47`) is the existing accessor and
should be reused rather than duplicated; its docstring already records the
"defaults to asm5 for an unrecognised value rather than erroring" behaviour
this needs.

**`launch_misassembly_qc` is the shape to copy** (`pipeline_service.py:4180`),
not `launch_scaffold`. Both take a draft plus a reference and both solved the
ambiguity this feature hits — a project holding two reference FASTAs is the
ordinary case, so the reference arrives from a dialog chooser rather than
being resolved silently — but `launch_misassembly_qc` is the closer analogue
in the way that matters: it is **read-only QC that produces facts, not an
object**, exactly like this. `launch_scaffold` produces a scaffolded assembly.

Four behaviours to copy from it verbatim:

- reference resolution with the "name the one to use" error when a project
  holds several (`:4211-4236`)
- reject draft-equals-reference (`:4248`)
- **reject draft and reference in different projects** (`:4243`) — a guard
  `launch_scaffold` does not have, and one this needs for the same reason
- **no run record.** `launch_misassembly_qc` enqueues directly with no
  `create_run` and no `link_job`, and so does `launch_completeness`
  (`:3633`). Both are read-only jobs merging facts onto an object that already
  exists, which is what a synteny alignment is too.

## The volume question, answered

The design pass expected binning or a minimum-length filter "chosen against a
real PAF, not guessed." The right answer is a **minimum alignment length
filter, and no binning** — and the reason is that a dot plot is not a
histogram.

PAF from an asm-preset alignment is already sparse. minimap2 at `asm5` emits
one record per collinear alignment block, not one per base or per k-mer — a
bacterial genome against a close reference produces thousands of records, not
millions. The volume risk is real only for fragmented drafts against distant
references, where the tail is thousands of short, low-confidence hits.

Those short hits are exactly what the plot should not draw. They are the
noise a dot plot is read *through*, and dropping them is a fidelity
improvement, not merely a size guard. So:

- **Filter: drop alignment blocks shorter than 1,000 bp** on the target axis.
  This is a floor on what is visually meaningful at the plot's scale — a
  1kb feature on a 5Mb genome is a fifth of a pixel — not a statistical
  judgement about the alignment.
- **Cap: `MAX_SYNTENY_SEGMENTS = 10_000`**, retaining the longest blocks when
  exceeded, with a **`synteny_segments_partial`** flag. This mirrors
  `_parse_gfa`'s `gfa_topology_partial` convention (`parsers.py:637`) rather
  than inventing a second vocabulary for "we kept some of it."

Retaining the **longest** rather than the first is the one detail worth being
explicit about: PAF is emitted in query order, so keeping the first 10,000
would keep everything from the first few contigs and nothing from the rest —
a biased sample that looks like a real finding (a genome that aligns only at
one end). Keeping the longest is unbiased with respect to position and keeps
precisely the blocks the plot is legible at.

**Do not bin into a 2D grid.** Binning is the right answer for a density plot
where overplotting hides the signal. Here the signal *is* the individual
segment's slope and direction — an inversion is a segment with negative slope,
and a binned grid destroys slope. The 1kb floor plus the 10,000 cap bounds
the data without touching what the plot reads.

### What gets stored

One fact on the draft assembly object, `synteny_alignment`, holding the
reference it was computed against and the segment list:

```
{
  "reference_object_id": "...",
  "reference_name": "GCF_000146045.2_R64_genomic.fna",
  "divergence": "same_species",
  "target_lengths": {"chrI": 230218, ...},   # reference contigs, for the X axis
  "query_lengths":  {"contig_1": 812430, ...},  # assembly contigs, for the Y axis
  "segments": [
    [target_name, target_start, target_end, query_name, query_start, query_end, strand],
    ...
  ]
}
```

Segments are **positional arrays, not objects**. Ten thousand records with
seven keys each is roughly 1.4MB of repeated key names against Mongo's 16MB
document cap; as arrays it is a fraction of that. `sequence_nx_curve` set this
precedent with `[percent, length]` pairs, and the frontend already destructures
that shape.

`target_lengths` and `query_lengths` are stored rather than derived because
the axes must span the full genome even where nothing aligned — an unaligned
reference chromosome is a finding, and an axis scaled only to the data would
silently crop it out of existence.

**Only one synteny alignment is kept per assembly.** A second run against a
different reference replaces it. Storing a list keyed by reference was
considered and rejected: it multiplies the document-size problem by the number
of references in a project, and the question the plot answers ("does my
assembly agree with *this* reference") is asked one reference at a time. The
dedup key below enforces the same thing at launch.

## Backend

**New `backend/app/pipelines/synteny_runner.py`**, following
`quast_runner.py`'s split: a `build_synteny_command` that constructs the
minimap2 invocation, and a `parse_paf` that turns its stdout into the segment
list. Both pure, both unit-testable without running minimap2 — which is the
whole reason the runners are split this way.

The command:

```
minimap2 -x asm5 --secondary=no -t <threads> <reference> <draft>
```

`--secondary=no` matters and is not decoration. minimap2 emits secondary
alignments by default; on a repeat-rich genome each repeat copy produces a
hit against every other copy, and those hits render as an off-diagonal
scatter that reads exactly like a translocation. Suppressing them is the
difference between a plot that shows structure and one that shows a cloud.

`parse_paf` reads PAF's first 12 mandatory columns. The ones this needs, by
0-based index: `0` query name, `2` query start, `3` query end, `4` strand,
`5` target name, `6` target length, `7` target start, `8` target end. PAF is
tab-separated with a variable number of trailing `tag:type:value` fields —
parse by index into the fixed prefix and ignore the tail. Coordinates are
0-based half-open on both axes, which is already what an SVG wants.

**New handler `analyze_synteny`** in `backend/app/queue/assembly_qc_handlers.py`,
`HandlerMode.SUBPROCESS`, `max_attempts=1` (deterministic tool on
deterministic input — the same reasoning `assess_completeness` gives at
`assembly_qc_handlers.py:51-54`). `JobResources(cpu=4, mem_mb=8192,
io=IoClass.LIGHT)`, matching `launch_scaffold`'s sizing for the same minimap2
whole-genome alignment.

**New `launch_synteny`** in `pipeline_service.py`, modelled on
`launch_misassembly_qc` including all four behaviours listed above.
`dedup_key=f"analyze_synteny:{draft.id}:{reference.id}"`, matching that
function's `assess_misassemblies:{draft.id}:{reference.id}` convention of
keying on the handler name plus both inputs.

**No new `RunKind`, no new `RunJobRole`, no run record** — see above. This is
worth stating as a decision rather than an omission, because "a new pipeline
feature gets a new `RunJobRole`" is the pattern most of the tree follows, and
the read-only QC launchers are the deliberate exception.

## Frontend

**New `frontend/src/components/SyntenyPlot.tsx`**, hand-rolled SVG, no
dependency. This is the ordinary case for this repo's charts, not the
cytoscape exception #150 made: a dot plot needs no computed layout — every
segment's position is given by its coordinates.

Rendering notes that are load-bearing:

- **Facet by reference contig on X, assembly contig on Y**, with thin
  separator lines between contig bands rather than one continuous axis.
  A single concatenated axis makes a contig boundary indistinguishable from a
  real break in alignment.
- **Order Y (assembly contigs) by their median target position**, not by name
  or length. Name order is arbitrary with respect to the reference, and it
  turns a perfectly collinear assembly into a scatter of disconnected bands —
  the plot's central signal destroyed by sort order alone.
- **Colour by strand**, so inversions are visible as colour, not only as
  slope. At a whole-genome zoom a short inverted segment's slope is not
  readable but its colour is.
- `aria-label` describing the comparison, per `BuscoChart.tsx` and
  `NxChart.tsx`.

Wired into `AssemblyFacts.tsx` beside `<NxChart>`, rendered only when
`synteny_alignment` is present. When absent the tab shows nothing — no empty
state and no disabled control, matching how NGx degrades silently. The run is
launched from the Actions tab, not from the chart's absence.

## Suggestion rule

Per CLAUDE.md's standing warning, a tool no rule can suggest never runs. A
card must be added to `suggestion_service.py` and a case to
`backend/tests/services/test_suggestion_service.py`.

The rule: offer synteny analysis when the project holds **a draft assembly and
at least one reference-role FASTA**. Two traps the Actions-tab rules have
already been bitten by, both recorded in CLAUDE.md and both live here:

- **`protein.faa` and `cds_from_genomic.fna` are FASTA but are not alignable
  references.** A rule keying on format alone will count them and offer to
  compare an assembly against a protein file.
- **The same assembly stored twice counts as two.** Deduplicate by digest
  before deciding the reference is ambiguous.

Check the rule against a real project with
`docker compose exec api python -c "..."` before believing the unit tests —
the suggestion rules passed a full green suite while getting both of the above
wrong.

## Testing

`build_synteny_command` and `parse_paf` are pure and get ordinary pytest
coverage:

- a PAF line with `+` strand and one with `-`, asserting strand survives
- a record with trailing `tag:type:value` fields, asserting the tail is ignored
- a sub-1kb block, asserting it is filtered out
- more than `MAX_SYNTENY_SEGMENTS` blocks, asserting the **longest** are kept
  and `synteny_segments_partial` is set — assert on which survive, not just
  the count, since keeping the first N passes a count-only assertion
- a malformed line (too few columns), asserting it is skipped rather than
  raising
- `build_synteny_command` includes `--secondary=no` and the preset the
  divergence maps to

Run from a worktree with `./backend/run-worktree-tests.sh tests/ -q`, never
`docker compose exec api python -m pytest` — that tests main's code, not the
worktree's.

Frontend verification is manual at localhost:5273 via `./ops/worktree-up.sh`.
The case worth constructing deliberately: an assembly with a **known
inversion**, to confirm the reversed segment renders on the opposite diagonal.
A plot that looks plausible on a clean assembly proves very little, since a
clean assembly is a straight line whether or not strand is handled correctly.

## Sequencing

Independent of #151 — different tool, different facts, different component.
Either can go first.
