# Variant genomic context in the NCBI Sequence Viewer

## Problem

The variants table answers "what changed" and nothing else. A row reads
`NC_000011.10  112,227,634  G  A  221.4` — coordinates with no surroundings.
The question it provokes, every time, is the one it cannot answer: *what gene
is this in, and what else is nearby?*

Today that means copying an accession and a position into NCBI by hand.

The app already embeds NCBI's Sequence Viewer (`SequenceViewerModal`), but only
from `ChromosomeStrip`, and only for a whole chromosome. It has no way to open
on a position.

## Goal

A **Context** button on each variant row that opens the Sequence Viewer on that
variant's chromosome, zoomed to a readable window, with a labeled marker on the
variant.

## What this is not

- **Not the Genome Data Viewer.** GDV was evaluated and rejected: its `id`
  parameter requires an NCBI *assembly* accession, which this app does not
  store, and it covers "annotated eukaryotic genome assemblies" only — useless
  for the bacterial and viral references this user works with. GDV also *wraps*
  the Sequence Viewer ("The core component of the GDV browser is NCBI's
  Sequence Viewer"), so it is not an alternative renderer but the same one
  behind a sidebar of navigation chrome that is redundant when the exact
  coordinate is already known.
- **Not iCn3D / 3D structure.** Blocked on a variant-annotation stage that does
  not exist; see "Follow-on work".
- **Not a track upload.** Showing the user's own BAM or VCF as a Sequence Viewer
  track would require those files to be publicly reachable by NCBI. They are
  local and should stay local.
- **Not a new outbound dependency.** This calls the *same* `sviewer.js` already
  loaded, with more parameters.

## Data available

Everything needed exists client-side today. No backend change, no pipeline
change, no new API field.

| Need | Source | Notes |
|---|---|---|
| Chromosome accession | `VariantRow.chrom` | Already rendered in the table |
| Variant position | `VariantRow.pos` | Already rendered, 1-based |
| Ref / alt alleles | `VariantRow.ref` / `.alt` | For the marker label |
| Sequence length | `VariantContigRow.length` | Already a prop of `VariantTable` |
| Accession test | `isNcbiNucleotideAccession()` | Already in `lib/chromosomes.ts` |

`VariantTable` already receives `contigs: VariantContigRow[]`, and that row type
already carries `length`. The window scaling below needs no new plumbing.

## The load string

Verified against NCBI's Sequence Viewer embedding API documentation.

Current call, unchanged:

```
embedded=true&appname=BioFlowLocalPipeliner&id=<accession>&tracks=[key:gene_model_track]
```

With a focus, two parameters are appended:

```
&v=<start>:<end>          visible range
&mk=<pos>|<label>|ff5555  marker at position
```

From NCBI's spec:

- `v=<view ranges>` — "sets a specified visible range to a graphical panel. If
  not specified the whole sequence is shown."
- `mk=<position or range>|<marker name>|<color in RGB hex>` — name and colour
  are optional; `mk=1000` alone is valid.

When no focus is supplied the string is byte-identical to today's, so
`ChromosomeStrip`'s existing behaviour is untouched by construction rather than
by test.

## Window scaling

A fixed flanking window cannot work across this user's range, which runs from
viruses to plants — four orders of magnitude of genome size. At ±10 kb a 10 kb
viral genome is shown whole (the zoom accomplishes nothing) while a plant gene
with 50 kb introns is cropped to a fragment (the gene structure, the entire
point, is off-screen).

The window is therefore a fraction of the containing sequence's length:

```
half = clamp(length * 0.01, 2_000, 200_000)
v    = [max(1, pos - half), min(length, pos + half)]
```

- **1%** of sequence length.
- **2 kb floor** so a small viral genome does not zoom to a near-empty view.
- **200 kb ceiling** so a plant chromosome does not become an unreadable smear.

These three constants are judgment, not measurement. They are starting points
chosen to degrade sensibly at both ends of the range, and should carry a comment
saying so — in the manner of `MAX_BAR_H` and `LOAD_TIMEOUT_MS` in the existing
code, which document their reasoning rather than presenting themselves as
derived.

**Fallback:** when the contig length is unknown (no matching `VariantContigRow`),
`v=` is omitted entirely. NCBI then shows the whole sequence and the marker
still lands correctly. The degraded path is the simple path, not a separate one.

## Gating

The button renders only when `isNcbiNucleotideAccession(row.chrom)` passes.

Variants are called against whatever reference the user aligned to, which is
frequently a local assembly with contig names like `contig_47` or
`scaffold_112`. Those have no page at NCBI. A button that opens a viewer which
then fails is worse than no button, and `ChromosomeStrip` already uses this
same helper for this same reason.

No tooltip, no disabled state — the cell is simply empty for such rows. A
disabled control invites a click and then explains why it was pointless.

## Marker label sanitisation

NCBI warns that special characters in marker names "must be escaped properly,"
and `|` is the field separator within `mk`.

A natural label for a variant — `G→A` — contains a non-ASCII arrow, and allele
strings come from a VCF, which permits characters (`<INS>`, `*`, long
sequences) that would corrupt the parameter.

Labels are therefore built conservatively:

- Format: `<ref>-to-<alt>`, ASCII only.
- Strip anything outside `[A-Za-z0-9-]`.
- Truncate each allele to a short bound (indels can be kilobases long; the
  marker label is not where that belongs).
- Fall back to `variant` if sanitising empties the string.

## Architecture

The modal is **generalised, not duplicated**. `SequenceViewerModal` already
solves the expensive problems — NCBI's two-stage loader, the `appname` cookie
warning, the absent teardown API and the `m_Apps` leak, the 15 s timeout, the
offline escape hatch. None of that is rewritten or copied.

### `SequenceViewerModal.tsx`

Gains one optional prop:

```ts
focus?: { position: number; label: string; sequenceLength?: number }
```

- When absent: current behaviour exactly.
- When present: append `v=` (unless `sequenceLength` is missing) and `mk=`.
- `focus` joins the load effect's dependency array, so clicking a second
  variant reloads the existing instance rather than leaving the first view up.
  The effect already tears down and rebuilds the host div, so this needs no new
  teardown logic.
- The heading shows the position alongside the accession when focused.

### `lib/chromosomes.ts`

Gains two pure, testable helpers:

- `focusWindow(pos, length)` → `[start, end]`, implementing the clamp above.
- `markerLabel(ref, alt)` → sanitised string.

Placed here rather than in the modal because they are pure logic with no NCBI
lifecycle involvement, and this is where the existing accession helper lives.

### `VariantTable.tsx`

- Builds a `Map<string, number>` of contig → length from the `contigs` prop.
- Adds a trailing `Context` column, gated as above.
- Holds the selected variant in state and renders `SequenceViewerModal` with a
  `focus` when set.

## Testing

Per CLAUDE.md there is no headless component-testing setup in this repo, and
none is expected. The pure helpers are the testable surface, and they are where
the logic actually lives:

- `focusWindow` — floor applies on a small viral genome; ceiling applies on a
  large plant chromosome; the window clamps at sequence start (`pos` near 1) and
  at sequence end; the 1% band applies in the middle.
- `markerLabel` — arrows and `|` are stripped; long indel alleles truncate;
  symbolic alts (`<DEL>`) survive as something non-empty; empty input yields the
  fallback.

Verification of the viewer itself is manual at localhost:5173, which is the
actual verification step for anything UI-facing in this repo:

1. A variant on an `NC_`/`NZ_` accession opens the viewer with the marker
   visible and on the right base.
2. A variant on a local contig shows no button.
3. Clicking a second variant moves the view rather than stacking.
4. `ChromosomeStrip`'s whole-chromosome viewer still opens unchanged.

## Follow-on work

**iCn3D (3D structure).** Deferred to its own spec. The blocker is not the
viewer but the data: `variant_runner.py` runs `bcftools mpileup → call → view`,
which emits CHROM/POS/REF/ALT with no `ANN`/`CSQ` consequence field, and no
annotator is registered in `tools.py`. Nothing can currently say which protein
or which residue a variant affects — the entire premise of a structure view.
That project is an annotation stage (`bcftools csq` is the cheap path: bcftools
is already probed, and it consumes the GFF3 already storable as
`ObjectRole.ANNOTATION`), plus a `suggestion_service.py` rule so the card is
reachable, with the viewer as the small final step. Its unresolved risk is
mapping gene → structure accession, which fails for most non-model organisms
and must degrade to "no structure available" as a normal outcome.

**MSA Viewer and Tree Viewer.** Rejected for now — no data source. Nothing in
the pipeline emits Newick or a multiple alignment, so both are viewers without
producers.
