# Annotation editing design

Issue #297 — edit annotation records.

The first slice covers GFF3 and GTF (the formats whose full column structure
the issue names — coordinates, type, source, attributes). BED and GenBank
are out of scope for this slice (no issue-mandated fields to edit; GenBank
has no line identity; BED's coordinate zero-base/one-base conversion is a
separate correctness concern).

## Problem

#257 makes annotation records inspectable but read-only. Users who identify
incorrect names, qualifiers, coordinates, or relationships must leave
BioFlow to correct them.

## Summary

Edit individual columns of an annotation's source lines (GFF3 / GTF only),
stored as a draft overlay against the source object. Materialize the overlay
into a derived annotation object via a queued job. Every step is validated;
identity keys are protected; the overwrite preserves every unedited column
verbatim (no "reconstruct from Feature" — same philosophy as export's
"re-emit source line").

---

## Requirements

### ED-1 — Editable columns

For GFF3 and GTF annotations, the following columns are editable per source
line. Every other column in the source line is preserved verbatim at
materialization time.

| Field      | GFF/GTF column | Validation                                            |
|------------|----------------|-------------------------------------------------------|
| `source`   | 2 (0-based 1) | Free text; must not contain tab or newline            |
| `type`     | 3 (0-based 2) | Non-empty; must not contain tab or newline            |
| `start`    | 4 (0-based 3) | Positive integer; start <= effective end              |
| `end`      | 5 (0-based 4) | Positive integer; effective start <= end              |
| `attributes`| 9 (0-based 8) | Valid format-specific string (`key=val;…` or `key "val";…`); must not contain tab or newline |

### ED-2 — Pre-materialization coordinate validation

When saving a `start` or `end` edit, the endpoint validates the effective
coordinate pair (the new value + the other coordinate's pending edit or,
when none, the parsed value from the index). `start <= end` is checked on
every save, so an edit that produces `start > end` is rejected before the
edit record is stored.

### ED-3 — Identity-key protection

Editing the `attributes` column must not change the `ID` or `Parent` keys
(GFF3) / `gene_id` or `transcript_id` keys (GTF). The server parses old and
new attribute strings, checks that every identity key has the same value
(or is absent from both), and rejects the edit otherwise.

This protects parent relationships by construction: no identity change ⇒
hierarchy unchanged.

### ED-4 — Durable edit overlay

Edits are stored in a Beanie `AnnotationEdit` document, one per (object,
line, field). `old_value` is the source-line column value at save time,
read from the source file. The unique index is (`object_id`, `line`,
`field`). An edit whose new value equals `old_value` deletes the record
(idempotent revert-to-original).

### ED-5 — Reviewable diff

The frontend renders a "Pending edits" panel listing every edit with
`field: old_value → new_value`. The edit records themselves are the diff —
nothing is computed at display time.

### ED-6 — Materialization

A queued job reads the source file + all pending edits for the source
object, rewrites the edited columns in each tagged line, and writes the
result to a temp file. The output is a valid GFF3 or GTF: every source line
except the edited columns is byte-for-byte identical.

On success the applier:
- Registers a derived `ObjectRole.ANNOTATION` object, `derived_from` the
  source, `produced_by_job` the materialization job.
- Stores `annotation_edit_count` and `annotation_edit_summary` (list of
  `{line, field, old_value, new_value}`) in the derived object's facts.
- Deletes the source object's pending edits from MongoDB.

### ED-7 — Re-parse check

After materialization the handler re-parses every edited line through the
format's parser (`parse_gff3_line` / `parse_gtf_line` + GXF parser). If any
line fails to re-parse, the job fails permanently — the edit produced an
invalid line despite pre-save validation.

### ED-8 — No auto-results

The derived object carries no pre-computed annotation stats, consistent
with export's #298 deferral. The user sees a "Compute results" affordance
on the new object.

### ED-9 — Edit per source line, not per table row

A GFF3 feature with `Parent=a,b` is one source line stored as two table
rows (one per parent). The edit overlay keys on source line, so editing
either row edits the same underlying feature. The frontend must use `line`
as the edit key, not `feature_id` or `parent`.

### ED-10 — The `line` column is exposed to the client

`annotation_db._COLUMNS` adds `line` so `AnnotationFeature.line` carries
the 1-based source line number, and the frontend can address features by it.

---

## Scope boundaries

- **In scope (first slice):** GFF3 and GTF only. Editable columns as listed
  in ED-1.
- **Out of scope:** BED (no issue-mandated fields beyond coordinates; zero-base
  conversion), GenBank (no line identity). The editing affordance is hidden
  for these formats.

---

## Components

### Backend

#### New Beanie model: `AnnotationEdit`
```
@Collection("annotation_edits")
AnnotationEdit
  object_id: Indexed(PydanticObjectId) — compound with line,field
  line: int — 1-based source line
  field: str — "source" | "type" | "start" | "end" | "attributes"
  old_value: str | None
  new_value: str
  owner: str
  created_at: datetime
  updated_at: datetime
Indexes: unique compound on (object_id, line, field)
```

#### New API routes (under `/annotationstats/edits/`)

- `GET /annotationstats/edits/{object_id}` → list of `{line, field, old_value, new_value}`.
- `PUT /annotationstats/edits/{object_id}` — body `{line, field, new_value}`, upsert one edit. Validates (ED-1, ED-2, ED-3), reads old-value from source, stores/removes.
- `DELETE /annotationstats/edits/{object_id}` — body `{line, field}`, remove one edit.
- `POST /annotationstats/materialize` — body `{object_id}`. Launches the materialization job.

#### New pipeline: `materialize_annotation_edits`

- `pipeline_service.launch_materialize_annotated_edits(object_id, owner)` — queue job.
- Handler (`pipeline_handlers.materialize_annotation_edits`):
  1. Load source object, resolve path via `_resolve_readable`.
  2. Load all edits for source from `AnnotationEdit` collection.
  3. Scan source, line by line, rewriting edited columns.
  4. Write to temp file.
  5. Re-parse every edited line (ED-7).
  6. Return `{output_path, edit_count, edit_summary}`.
- Applier: call `ingest_local_file` with `derived_from`, `role=ANNOTATION`, `facts` carrying edit summary, then delete edits.

#### Modification: `annotation_db._COLUMNS` adds `line`

### Frontend

#### API client additions

```typescript
api.annotationEdits: (objectId: string) => Promise<AnnotationEdit[]>
api.saveAnnotationEdit: (objectId: string, edit: {line: number, field: string, new_value: string}) => Promise<void>
api.deleteAnnotationEdit: (objectId: string, line: number, field: string) => Promise<void>
api.materializeAnnotationEdits: (objectId: string) => Promise<JobSummary>
```

#### Types additions

```typescript
interface AnnotationEdit {
  line: number;
  field: string;
  old_value: string | null;
  new_value: string;
}
```

#### UI components

1. **`AnnotationPendingEdits`** — renders the edit diff (ED-5) with a
   "Materialize" button. Shows each edit as `field: old → new`, with a
   remove affordance per edit. Only visible when there are edits.

2. **`AnnotationEditInline`** — inline row editor. Renders in-place when a
   row's edit button is clicked. Shows editable fields (ED-1) as inputs,
   with Save/Cancel. Hidden for BED and GenBank rows (gated on
   `facts.gff_version`).

3. **`AnnotationFeatureTable`** modifications:
   - Pass `facts` deeper so `FeatureRow` can gate editing.
   - Add `line` to the API query response and thread it to `AnnotationFeature`.
   - Add an edit button column to feature rows (for GFF/GTF, non-genes views).

---

## Testing

- **Validation tests:** Each ED-1 field's validation rules: positive int for
  coordinates, non-empty type, identity-key protection for attributes,
  tab/newline rejection.
- **Materialization test:** Edit two lines (one coordinate, one type),
  materialize, verify output byte-for-byte against expected.
- **Export contract test:** Verify edited output re-parses without errors
  (ED-7).
- **No-regressions on existing routes:** features, children, window, genes,
  export-count, export.