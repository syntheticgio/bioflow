# Converting reads files into references

Date: 2026-07-26
Status: Approved, ready for implementation planning

## Problem

A reference genome and a set of sequencing reads can be byte-for-byte the same
kind of file: FASTA or FASTQ. Which one a file *is* depends on how the user
intends to use it, not on anything detectable in its contents. The system
currently has no way to record that intent.

The concept is already half-present. `ProjectExplorer.tsx` categorizes a file as
a reference when `metadata.is_reference === true`, and `metadata/schemas.py`
defines `REFERENCE_FIELDS`. But nothing ever sets `is_reference`, the reference
fields are keyed to `FormatKind.FASTA` rather than to the reference concept, and
the detail panel renders every file identically. The user has no control that
makes the distinction.

## Goal

From the detail panel of a reads file, the user can declare it a reference. The
file moves into the REFERENCE section of the left panel, and the panel shows
metadata and parsed facts appropriate to an assembly rather than to a
sequencing run. The conversion is reversible.

## Decisions

The following were settled during design; they are recorded with their reasoning
because the reasoning constrains the implementation.

**Role is a first-class field, not metadata.** `metadata.is_reference` is
abandoned in favour of `DataObject.role`. Classification that drives which
section a file appears in, which metadata schema applies, and which facts are
displayed should not live in the same free-form bag as user notes, where it can
be cleared by accident from the metadata editor. No migration or backfill is
required: no reference files exist yet.

**Role is an override, not a universal label.** `role` defaults to `None`,
meaning "derive the category from the detected format," which is what happens
today. Only exceptions carry a value. The format already yields the correct
category in every case except reference-vs-reads — precisely the ambiguity being
solved. Keeping the rest derived means re-ingest can never fight a user's
choice, and there is nothing to backfill.

**Role is an enum, not a boolean.** A single member today. When WIG support
lands, formats with several plausible roles extend the enum without a schema
change.

**Role replaces format for schema selection, rather than adding to it.** Once a
file is declared a reference, library and sequencing questions — insert size,
flowcell, lane — stop being meaningful. It is a genome build, not a run.
Showing those fields is noise.

**Converting does not trigger a re-ingest.** Relabelling a file does not change
its bytes, and every fact a reference display needs is already extracted on
first ingest. Conversion is a pure metadata write: fast and synchronous. The
existing re-ingest button remains available if a parser later improves.

**Conversion is reversible with no confirmation dialog.** Both directions are
the same operation with a different value. Because it is cheap and reversible, a
confirm step is friction without benefit.

## Data model

`backend/app/models/object.py`:

```python
class ObjectRole(StrEnum):
    REFERENCE = "reference"
```

`DataObject` gains `role: ObjectRole | None = None`.

Index: add `IndexModel([("project_id", ASCENDING), ("role", ASCENDING)],
name="by_role")`. Cheap now, and the search layer will want to filter on it.

## API

No new endpoint. Conversion reuses `PATCH /objects/{id}`, consistent with how
rename and tag edits already work.

- `ObjectUpdate` gains `role: ObjectRole | None = None`.
- `ObjectOut` gains `role: str | None`.

**Null-versus-omitted is load-bearing.** `update_object` calls
`body.model_dump(exclude_unset=True)`. An explicitly sent `{"role": null}` is
included in that dump; an omitted `role` is not. This is exactly what makes
"convert back to reads" work through the same endpoint that leaves role
untouched during an unrelated rename. Both cases require a test:

- `PATCH {"role": "reference"}` sets the role.
- `PATCH {"role": null}` clears it.
- `PATCH {"name": "x"}` on an object with a role leaves the role intact.

## Metadata schema

`fields_for()` gains an optional role parameter, and role short-circuits format:

```python
def fields_for(kind, role=None):
    specific = REFERENCE_FIELDS if role is ObjectRole.REFERENCE \
               else FORMAT_FIELDS.get(kind, ())
```

`field_map()`, `coerce_and_validate()`, and `schema_for_api()` each thread role
through.

**`FORMAT_FIELDS[FormatKind.FASTA]` changes from `REFERENCE_FIELDS` to `()`.** A
plain FASTA is no longer assumed to be a reference; it gets COMMON fields only.
`FASTQ` is unaffected and keeps `FASTQ_FIELDS`.

**Values from the previous role survive conversion.** If a reads FASTQ with
`flowcell` and `lane` filled in becomes a reference, those keys leave the active
schema. The existing validation path already handles this correctly:
`coerce_and_validate` falls back to `all_known_fields()`, and unknown keys are
stored verbatim by design. The old values appear under "Custom fields" in the
editor rather than being lost, so converting back restores the original form
exactly. This behavior should be covered by a test, since it is what makes the
toggle safely reversible.

### REFERENCE_FIELDS

Existing three retained, four added. Group: `Reference`.

| Key | Type | Suggested | Note |
|---|---|---|---|
| `reference_build` | text | yes | Free text, not enum — builds are open-ended (custom assemblies, patches) |
| `source` | text | yes | Ensembl release 110, UCSC, NCBI RefSeq |
| `masked` | boolean | no | Repeat-masked sequence |
| `assembly_accession` | text | yes | e.g. GCA_000001405.29 — the unambiguous identifier |
| `is_primary_assembly` | boolean | yes | Whether alt/patch contigs are excluded; a real source of alignment surprises |
| `has_decoy` | boolean | yes | Decoy contigs present (hs38d1); affects aligner choice |
| `index_types` | text | yes | Which aligner indexes exist (BWA, bowtie2, STAR) |

`organism` is deliberately not duplicated here; it already exists in
`COMMON_FIELDS`.

Additional fields can be added later with no migration, since unknown keys are
stored verbatim.

## Frontend

### Left panel

`categorizeFile` in `ProjectExplorer.tsx` drops the `metadata.is_reference`
check:

```ts
obj.role === "reference" ? "references" : categoryFromFormat(obj.format.kind)
```

The `references` category and its slot in `CATEGORIES` already exist, so the
section appears with no further change. Reference rows take a distinct icon
(📗) to stay scannable.

### Detail panel

`ObjectDetail` branches on role. The header badge reads **Reference** rather
than **File**.

**Unchanged, role-independent:** Format, Storage, Tags, Record, Delete. A
reference's hash, ref-count, and dedup story are the same concern as any other
file's.

**"Parsed facts" becomes "Assembly"** for references — a curated summary rather
than a `FactsTable` dump:

| Row | Source | Note |
|---|---|---|
| Sequences | `facts.sequence_count` / `sequence_count_estimate` | Render "~N (estimated)" when `sequence_count_exact` is false; the parser caps exact counting at 256 MB |
| Total bases | `facts.total_bases` | Formatted Gb/Mb. Absent when the count was truncated |
| GC content | `facts.gc_content_percent` | See sampling caveat below |
| Contig list | `facts.sequence_names` | First ~25 with a "show all" toggle; surface `sequence_names_truncated` honestly as "list truncated during parsing" |

**GC is sampled, and must be labelled as such.** `sequence_stats.fasta_stats`
caps at `max_bases=50_000_000`. On a multi-GB genome the figure comes from a
prefix — and the prefix of a reference is chr1, not a random sample. The row is
labelled "GC content (sampled)" and shows `facts.stats_sampled_bases` alongside
it, rather than implying a genome-wide figure.

**No longest/shortest sequence row.** Verified against `sequence_stats.py` and
`_parse_fasta`: per-sequence lengths are not extracted — `fasta_stats` counts
bases in aggregate and `_parse_fasta` collects names only. Adding them would
mean new parser work, which is out of scope here.

**Base composition chart: kept.** Informative for an assembly (GC skew,
N-fraction). `fasta_stats` returns `base_composition` under the same key as the
FASTQ path, so the existing component works unchanged.

**Quality-per-position chart: dropped.** A FASTA carries no per-base qualities.

**`SraPanel`: hidden for references.** SRA run and experiment accessions are the
wrong archive for an assembly. Instead, `assembly_accession` links out to NCBI,
which requires extending `ACCESSION_LINKS` in `lib/format.ts`:

```ts
assembly_accession: {
  pattern: /^GC[AF]_\d{9}\.\d+$/i,
  url: (v) => `https://www.ncbi.nlm.nih.gov/datasets/genome/${v}`,
  label: "Assembly",
}
```

### Conversion control

A section above Delete:

- **Convertible file** (FASTA or FASTQ, role unset): "Convert to reference",
  with the line "Marks this as a reference genome. It will move to the
  References section and show assembly metadata."
- **Reference:** "Convert back to reads", phrased so it is clear nothing is
  lost.
- **Anything else** (BAM, VCF, BED): the section does not render. Converting a
  VCF to a reference is not meaningful, and offering it invites confusion.

On success: invalidate `["object", id]` and `["objects", projectId]` so the left
panel re-sections immediately, and `notify.success`.

The schema query key must include role — `["metadata", "schema", formatKind,
role]` — or a conversion would serve the previous role's cached form.

## Testing

- Model: role defaults to `None`; enum round-trips through Beanie.
- API: the three PATCH cases above (set, clear, leave-untouched).
- Schema: `fields_for(FASTQ, role=REFERENCE)` returns COMMON + REFERENCE and no
  library fields; `fields_for(FASTA, role=None)` returns COMMON only.
- Schema: metadata keys from a previous role survive a round-trip conversion.
- Frontend: `categorizeFile` sorts on role ahead of format.

## Out of scope

- Multi-role formats (WIG). The enum accommodates them; nothing is built now.
- Per-sequence length extraction.
- Bulk conversion from the multi-select bar. Single-file only for this pass.
- Any change to search filtering on role, beyond adding the index that would
  support it.
