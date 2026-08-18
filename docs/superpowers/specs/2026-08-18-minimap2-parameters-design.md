# Curated Minimap2 parameters

**Issue:** [#570](https://github.com/syntheticgio/bioflow/issues/570)  
**Date:** 2026-08-18  
**Status:** design approved in discussion; written-spec review pending

## Problem

BioFlow currently exposes Minimap2's read-type preset (`-x`) plus shared
alignment settings, but does not expose the most useful Minimap2 tuning and
output controls. Users who need to adjust sensitivity, secondary alignments,
or alignment tags must choose between an under-configurable dialog and manual
work outside BioFlow.

The implementation must preserve the meaning of Minimap2's read-type presets.
An unset advanced field must mean "use the selected preset's Minimap2 default,"
not "apply a new BioFlow-wide numeric default."

## Scope

### Included in v1

Minimap2-specific fields are added to the existing registry-driven parameter
form and command builder.

Biology and sensitivity fields:

| UI field | Minimap2 flag | Purpose |
|---|---|---|
| K-mer size | `-k` | Controls seed size and sensitivity |
| Window size | `-w` | Controls minimizer density |
| Minimum chain score | `-m` | Filters weak chains |
| Maximum minimizer gap | `-g` | Controls chaining across gaps |
| Secondary alignment ratio | `-p` | Sets the minimum secondary/primary score ratio |
| Maximum secondary alignments | `-N` | Limits reported secondary hits |

Computational and output fields:

| UI field | Minimap2 flag | Purpose |
|---|---|---|
| Secondary alignment mode | `--secondary` | Selects tool default, enabled, or disabled behavior |
| Batch size | `-K` | Controls reads processed per batch |
| Supplementary soft clipping | `-Y` | Controls supplementary alignment clipping |
| `cs` tag output | `--cs` | Emits short or long difference tags |
| `MD` tag output | `--MD` | Emits MD tags for downstream tools |

Existing shared fields remain unchanged: read-type preset, threads, sort
memory, and duplicate marking.

### Deferred from v1

The first version does not expose alignment scoring (`-A`, `-B`, `-O`, `-E`),
chaining/alignment bandwidth (`-r`), Z-drop parameters (`-z`), index batch
size (`-I`), homopolymer-compression and low-level seeding controls,
splice-specific options, or low-level memory-capacity flags. These options are
either tightly coupled, index-only, difficult to explain safely, or likely to
cause users to override the read-type preset incorrectly.

## Decisions

### Preserve preset defaults

New Minimap2 tuning fields are optional overrides. `None`/blank means that the
flag is omitted and Minimap2 uses the default implied by the selected `-x`
preset. The command builder must never emit a fixed BioFlow value for an
unset optional override.

The existing read-type preset remains the primary biological configuration.
Changing it must not be silently neutralized by serialized defaults for the
new fields.

### Use the existing registry-driven form

The backend `ParamField` metadata remains the source of truth for field names,
types, defaults, choices, help text, and grouping. The frontend continues to
render `AlignerSchema.fields` through `AlignerParamFields`; it does not gain a
Minimap2-specific form component.

### Keep command construction deterministic

The command builder appends explicitly selected Minimap2 overrides in a stable
order after the required base arguments (`-a`, `-x`, `-t`, and `-R`). This keeps
provenance readable and makes exact command tests reliable.

### Make secondary-alignment semantics explicit

The secondary-alignment mode is tri-state:

- tool default: emit no `--secondary` flag;
- enabled: emit `--secondary=yes`;
- disabled: emit `--secondary=no`.

`-N` and `-p` are emitted only when explicitly configured. Help text must
explain that those values matter when secondary alignments are enabled.

### Keep saved parameter sets safe

Parameter-set eligibility continues to derive from `ParamField` metadata. New
optional fields that are unset are not persisted as explicit overrides. A
saved set therefore cannot accidentally replace a later read-type preset's
tool defaults with values the user never selected.

Existing drift detection remains in force: obsolete, invalid, or incompatible
saved values are reported visibly rather than silently discarded.

## Architecture and data flow

1. `backend/app/pipelines/align_params.py` adds typed optional Minimap2 fields
   and validates their ranges and choices.
2. `backend/app/pipelines/aligner_registry.py` declares those fields on the
   Minimap2 `AlignerSpec`, including labels, help text, groups, and schema
   metadata.
3. The schema endpoint serializes the metadata without a second hand-written
   projection.
4. `frontend/src/components/AlignDialog.tsx` passes the schema fields to
   `AlignerParamFields`, which renders blank optional values as tool defaults.
5. `backend/app/pipelines/align_runner.py` emits only selected overrides in a
   stable order.
6. Parameter-set saving filters out unset optional values; parameter-set
   resolution validates retained values against the current field metadata.
7. The run records the effective parameters and command provenance through the
   existing alignment execution path.

If the current field-kind model cannot represent `-p` as a validated numeric
value, add a general float field kind to the registry schema rather than
creating a Minimap2-only validation path.

## Validation and error handling

- Integer options reject values below their documented lower bounds.
- The secondary ratio rejects values outside Minimap2's valid numeric range.
- Select fields reject values outside their declared choices.
- Invalid parameter combinations fail during normal alignment validation,
  before the job is queued.
- A saved parameter containing a removed field, wrong type, invalid range, or
  withdrawn choice follows the existing parameter-set drift response and is
  shown to the user with the affected key and reason.
- Unset optional fields are omitted from the command rather than converted to
  zero, false, or a fixed fallback.

## Testing

### Backend parameter tests

Add coverage that Minimap2 defaults preserve preset behavior, each new field
round-trips through `from_dict` and `as_dict`, invalid values raise
`ValidationError`, unset values are omitted from command construction, and
each configured value reaches the command exactly once.

Explicit command tests cover secondary modes, `-N`, `-p`, `--cs`, `--MD`, `-Y`,
`-K`, `-k`, `-w`, `-m`, and `-g`.

### Registry schema tests

Verify that every Minimap2 field is present, has the expected kind, bounds,
choices, group, and non-empty help text. Verify nullable fields serialize
correctly.

### Parameter-set tests

Verify that unset Minimap2 overrides are not persisted, explicit overrides are
applied, changing the read-type preset does not inherit accidental fixed
defaults, and obsolete or invalid saved values produce visible rejection
details.

### Manual UI verification

Because this repository has no headless frontend component-test setup, verify
the worktree stack manually with `./ops/worktree-up.sh` on port 5273:

- Minimap2 displays the new grouped fields.
- Blank fields are visibly represented as Minimap2 defaults.
- Changing the read-type preset does not unexpectedly populate overrides.
- Representative short-read and long-read launches construct the expected
  commands.
- Recorded parameters and provenance reflect the selected overrides.

## Acceptance criteria

1. A user can configure the included Minimap2 fields from the alignment
   dialog without editing code or invoking a shell manually.
2. An unset advanced field leaves the selected Minimap2 preset in control of
   that setting.
3. Every configured field is validated before queueing and appears in the
   generated Minimap2 command exactly as configured.
4. Secondary-alignment, `cs`, and `MD` behavior is explicit in the UI and
   command construction.
5. Saved parameter sets preserve explicit Minimap2 overrides without
   serializing unset values.
6. Invalid or obsolete saved values are surfaced through the existing drift
   handling.
7. Backend tests pass, and the manual alignment-dialog verification succeeds.

## Implementation sequence

1. Add and validate the Minimap2 parameter fields.
2. Add registry metadata and schema coverage.
3. Update command construction and command tests.
4. Update optional-value handling in parameter-set persistence.
5. Render and explain the fields in the alignment dialog.
6. Run backend tests and perform manual UI verification.
7. Update alignment documentation if the final help text introduces a new
   user-facing convention.

