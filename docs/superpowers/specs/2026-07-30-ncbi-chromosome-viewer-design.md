# Chromosome strip and NCBI Sequence Viewer for references

Date: 2026-07-30

## Problem

The Quality tab shows a reference assembly's numbers -- sequence count, total
bases, GC -- but nothing about its *shape*. Whether a FASTA holds 16 chromosomes
or 8,769 coding sequences is a fact you can only infer by reading the sequence
count and guessing.

NCBI's Datasets genome page answers this visually with a strip of
proportionally-sized chromosome bars, and NCBI separately publishes a Sequence
Viewer embedding API that renders gene-model tracks for a single accession.

On a reference the per-position quality curve is deliberately suppressed
(`!isReference` in `QcTab`), so Base Composition currently sits alone in the
`.qc-charts` grid. The second column is literally empty today.

## What this is not

The chromosome strip on the Datasets page is **not** part of the Sequence Viewer
embedding API. It belongs to the Datasets genome page. This design builds two
separate things:

1. **The strip** -- drawn by us, from data already in `facts`. No NCBI calls.
2. **The viewer** -- NCBI's embedded `sviewer.js` widget, for one accession at a
   time, opened on demand.

## Scope

Frontend only. `sequence_names` and `sequence_lengths` are already ingested and
already serialized to the client. **No backend changes.**

## Data available

Confirmed against the live database, not assumed. The four objects with
`role == "reference"`:

| Object (`_id` prefix) | `sequence_count` | `sequence_lengths` | `ncbi_assembly_accession` | First name |
|---|---|---|---|---|
| `6a6a3416` `GCF_000146045.2_R64_genomic.fna` | 17 | 17 entries | `GCF_000146045.2` | `NC_001133.9` |
| `6a6a9b75` `GCF_000002445.2_ASM244v1_genomic.fna` | 12 | 12 entries | `GCF_000002445.2` | `NC_008409.1` |
| `6a6a340f` `GCA_000146045.2_R64_genomic.fna` | 16 | **absent** | none | `BK006935.2` |
| `6a664d69` `GCF_000002445.2_ASM244v1_genomic.fna` | 12 | **absent** | none | `NC_008409.1` |

The two `GCF_000002445.2` rows are distinct objects with the same filename in
the same project -- one ingested before `sequence_lengths` was added and one
after. They are a useful pair: identical files landing in different buckets
purely by ingest vintage.

Two further objects are reference-adjacent and matter as negative cases:
`cds_from_genomic.fna` (8,769 records named `lcl|NC_008409.1_cds_XP_846376.1_2`
-- local identifiers NCBI cannot resolve) and `protein.faa` (8,758 `XP_` protein
accessions). `genomic.gff` has no sequence names at all.

Two facts drive the whole design:

- **`sequence_lengths` is a complete name->length map** where present, so the
  strip needs zero NCBI round-trips.
- **Half the existing references lack it**, having been ingested before the
  field was added. That is a real state needing a real message, not a
  hypothetical.

## Architecture

Three new frontend files:

- **`frontend/src/lib/chromosomes.ts`** -- pure logic, no React. Classifies a
  reference and ranks its bars. Unit-tested with Vitest, as `lib/readQuality.ts`
  already is.
- **`frontend/src/components/ChromosomeStrip.tsx`** -- draws bars, handles
  overflow and selection, renders degraded states.
- **`frontend/src/components/SequenceViewerModal.tsx`** -- lazily loads
  `sviewer.js`, mounts the viewer, provides the escape-hatch link.

`DetailPanel.tsx` gains one `<div className="qc-chart">` beside Base
Composition, guarded by `isReference`. That file is already 1051 lines, so the
logic lives in the new modules rather than inline.

### Layout

`.qc-charts` is already `grid-template-columns: repeat(auto-fit, minmax(320px,
1fr))`. Adding a second `.qc-chart` child places it to the right of Base
Composition automatically and reflows to stacked on narrow panels. **No grid CSS
changes needed**; new rules are for the bars themselves.

## Classification logic (`lib/chromosomes.ts`)

One exported function returning a tagged union, so the caller renders per tag
and cannot forget a case:

```ts
type ChromosomeView =
  | { kind: "drawable"; bars: Bar[]; overflow: Bar[]; linkable: boolean }
  | { kind: "needs-qc" }
  | { kind: "not-chromosomal"; reason: string }
  | { kind: "nothing" };

interface Bar { name: string; length: number }
```

Resolved in order, each step falling through:

1. **No `sequence_names` and no `sequence_lengths`** -> `nothing`. (`genomic.gff`)
2. **Names present, `sequence_lengths` absent or empty** -> `needs-qc`. (the two
   pre-existing files above)
3. **Shape test.** Fewer than 5 sequences of at least 100 kb ->
   `not-chromosomal`. `cds_from_genomic.fna` (8,769 records, longest ~15 kb) and
   `protein.faa` both fail cleanly. `reason` is derived from what the file
   actually looks like, e.g. "8,769 sequences, none over 100 kb -- this looks
   like coding sequences or proteins, not chromosomes."
4. **Otherwise `drawable`.** Sort every sequence by length descending, take the
   top 24 as `bars`, the remainder as `overflow`.

`linkable` is a separate test applied to `drawable` only: do the names match
resolvable NCBI *nucleotide* accession shapes (`NC_`, `NZ_`, `NT_`, `NW_`, `CM`,
`CP`, `BK`, `AE`, plus generic two-letter + six-digit + version), and is at least
one resolvable. This is what keeps `lcl|...` and `XP_...` out of the viewer.

### Deliberate choices

- **Bars are ranked by length, not file order.** NCBI's page labels chromosomes
  I..XVI; we cannot recover roman numerals from `NC_001133.9` without a lookup
  this design has ruled out. Length-ranking is honest about what we know and is
  what makes the top-24 rule meaningful -- for a human assembly the 24 longest
  sequences are the primary chromosomes. Bars are labeled with the bare
  accession; hover gives name and formatted length.
- **The 100 kb threshold is a file-level test only, never a per-bar filter.**
  Yeast's mitochondrion `NC_001224.1` (85 kb) is under it but still gets a bar.
  Nothing in the file is silently dropped; anything past 24 goes to the overflow
  control, not away.
- **Two independent tests, by design.** Step 3 is local and decides whether to
  draw at all; `linkable` concerns NCBI resolvability and decides only whether
  bars click through. A locally-assembled genome with arbitrary contig names
  still gets its strip, with inert bars.

This mirrors the failure the Actions-tab suggestion rules hit: FASTA-ness is not
chromosome-ness, and a naming test alone would have admitted `protein.faa`.

## The strip (`ChromosomeStrip.tsx`)

**Bars.** Inline SVG, no charting library -- the approach `SequenceCharts.tsx`
already uses. Height proportional to length against the longest bar, drawn as
rounded vertical capsules, laid out left to right with the accession beneath.
Bar width is fixed so 24 bars wrap to a second row in a narrow column rather
than shrinking to slivers. Hover highlights the bar and shows name plus length
via `formatBases`. Colors come from existing theme tokens so this inherits
Broadsheet styling rather than hardcoding NCBI's palette.

**Section title:** "Chromosomes" when `drawable`, "Sequences" otherwise. Calling
8,769 CDS records chromosomes would be the same category error the logic exists
to prevent.

**Selection.** Clicking a bar opens the modal for that accession. When
`linkable` is false, bars still draw and still hover but are not buttons, with
one line beneath: "Sequence names aren't NCBI accessions, so these can't be
opened at NCBI."

**Overflow.** When `overflow` is non-empty, a `<select>` beneath the bars
labeled "...and N more", listing each remaining sequence by name and length.
Choosing one opens the modal for it.

**Degraded states**, each replacing the bars entirely:

- `needs-qc` -> "Sequence lengths weren't measured for this file. Re-run QC to
  draw the chromosome map." This component owns **no** re-run button: `QcTab`
  already renders `runQcPrompt` above the charts for exactly this, and the
  message points at it rather than duplicating a second trigger.
- `not-chromosomal` -> the `reason` sentence from step 3.
- `nothing` -> render `null`. The tab's existing "No header facts extracted"
  copy already covers it.

## The viewer modal (`SequenceViewerModal.tsx`)

**Script loading.** `sviewer.js` is injected on first open, never at page load:
a `loadSviewer()` helper appends the `<script>` tag once, caches the promise, and
resolves when NCBI's global appears. Later opens reuse it. Nothing else in the
app depends on NCBI being reachable -- which matters, because this is otherwise
a local-only tool and this script is its one runtime outbound dependency.

**Mounting.** The declarative form -- render

```html
<div class="SeqViewerApp" data-id="NC_001133.9" data-width="..."
     data-tracks="[key:gene_model_track]"></div>
```

and let the script claim it -- rather than the programmatic
`SeqView.App.AppNode` API. We are not driving the viewer from elsewhere in the
app, so the programmatic control buys nothing. Each open mounts a fresh div with
a unique React key so the script is never handed a node React has since reused.

**States**, all three of which must be handled because of the outbound
dependency:

- *loading* -- spinner while the script fetches
- *failed* -- script failed or timed out (offline, blocked, NCBI down) ->
  "Couldn't load the NCBI Sequence Viewer", with the escape-hatch link, which
  still works
- *loaded* -- viewer renders

**Escape hatch.** Always in the modal header regardless of state: "View at NCBI"
linking to the Datasets page for that accession, `target="_blank"
rel="noreferrer"`.

`lib/format.ts`'s `accessionUrl()` keys off a *field name* (`assembly_accession`,
etc.) and has no entry for a bare nucleotide accession, so this adds one to
`ACCESSION_LINKS` rather than hand-building the URL at the call site.

**Sizing and chrome.** A wide overlay -- giving the viewer room is the reason it
is a modal rather than inline in the column. Reuses the existing
`.modal-backdrop` / `.modal` / `.modal-body` structure from `AlignDialog`
rather than inventing a pattern; Escape and backdrop click close it.

## Features deliberately excluded

- **Coordinate deep-linking** (`data-v` to a region). Nothing in the app
  currently produces a locus to point at.
- **Configurable track sets.** Configuration for its own sake in a QC tool.
- **Programmatic `SeqView.App.AppNode` initialization.** No caller needs it.
- **Inline tracks under the strip.** The viewer needs width the Quality column
  does not have.

## Testing

`lib/chromosomes.ts` gets Vitest cases mirroring the real objects recorded
above -- the four in the table plus the three reference-adjacent negative cases:

| Fixture | Expected `kind` |
|---|---|
| `GCF_000146045.2_R64_genomic.fna` (17 seqs, lengths present) | `drawable`, `linkable: true` |
| `GCA_000146045.2_R64_genomic.fna` (names, no lengths) | `needs-qc` |
| `cds_from_genomic.fna` (8,769 `lcl\|...` records) | `not-chromosomal` |
| `protein.faa` (8,758 `XP_` accessions) | `not-chromosomal` |
| `genomic.gff` (no sequence names) | `nothing` |

Plus one synthetic case with no counterpart in the current data: a
chromosome-scale assembly whose contig names are *not* NCBI accessions, which
must come back `drawable` with `linkable: false`. This is the local-assembly
path, and nothing in the live database exercises it.

Per CLAUDE.md, assertions run in the direction that fails when the logic breaks
-- that CDS and protein files are *rejected*, and that missing lengths yield
`needs-qc` -- rather than only confirming the happy path. Fixtures are built
from the real field shapes recorded above, not hand-made objects that already
look the way the rules expect.

The strip and modal are verified manually in the browser at localhost:5173.
There is no headless component-testing setup in this repo and none is expected.
