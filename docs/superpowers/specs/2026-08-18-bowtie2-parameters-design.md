# Bowtie2 Parameter Controls and Workflow Presets

**Issue:** #567  
**Status:** Design approved in conversation; implementation not started  
**Source:** `syntheticgio/bioflow#567`, the Bowtie2 manual, and the existing
aligner parameter architecture in `backend/app/pipelines/` and
`frontend/src/components/`.

## Problem

The Bowtie2 dialog exposes sensitivity, local alignment, maximum insert size,
mixed/discordant-pair suppression, and bounded multi-mapping. It does not yet
expose the minimum insert size, mate orientation, or pair geometry controls
that users need for different paired-end library designs. Issue #567 also
asks for organism-specific defaults, but taxonomy alone does not determine
insert-size distribution, mate orientation, read quality, divergence, or
whether discordant and multi-mapping alignments are useful.

The design therefore uses explicit workflow/read-characteristic presets rather
than inferred organism categories.

## Goals

- Give paired-end users control over the Bowtie2 fragment-size range and mate
  orientation.
- Expose useful pair-geometry controls without presenting all Bowtie2 scoring
  internals as routine form fields.
- Provide conservative, explicit presets for common library/workflow shapes.
- Preserve current defaults and command behavior unless a user selects a
  preset or changes a field.
- Keep parameter metadata, validation, command construction, UI rendering,
  and provenance aligned through the existing registry architecture.

## Non-goals

- Infer alignment parameters from organism taxonomy or project metadata.
- Automatically infer a preset from assay metadata.
- Expose the complete Bowtie2 scoring and seed-search surface in this change,
  including `--score-min`, `--ma`, `--mp`, `--np`, `--rdg`, `--rfg`, `-D`, `-R`,
  `-N`, and `-L`.
- Change Bowtie2 index construction.

## Requirements

### Parameter model

- **B2P-001:** The system must accept a Bowtie2 minimum insert size named
  `minins`, defaulting to `0`.
- **B2P-002:** The system must retain `maxins`, defaulting to `500`.
- **B2P-003:** The system must accept mate orientation values `FR`, `RF`, and
  `FF`, defaulting to `FR`.
- **B2P-004:** The system must accept `dovetail`, `no_contain`, and
  `no_overlap` boolean controls, each defaulting to false.
- **B2P-005:** The system must retain bounded multi-mapping through `report_k`
  and add an all-alignments control named `report_all`.
- **B2P-006:** The system must reject a `minins` value greater than `maxins`.
- **B2P-007:** The system must reject orientation values outside `FR`, `RF`,
  and `FF`.
- **B2P-008:** The system must reject negative `report_k` values.
- **B2P-009:** The system must reject requests that enable both `report_k`
  with a value greater than zero and `report_all`.

### Command construction

- **B2C-001:** For paired-end input, the runner must emit `-I <minins>` when
  `minins` is greater than zero.
- **B2C-002:** For paired-end input, the runner must emit `-X <maxins>` using
  the resolved `maxins` value.
- **B2C-003:** For paired-end input, the runner must emit exactly one mate
  orientation flag corresponding to the resolved orientation.
- **B2C-004:** For paired-end input, the runner must emit `--dovetail`,
  `--no-contain`, and `--no-overlap` only when their respective controls are
  enabled.
- **B2C-005:** The runner must emit `-k <N>` when `report_k` is greater than
  zero and must emit `-a` when `report_all` is enabled.
- **B2C-006:** For single-end input, the runner must omit mate orientation,
  insert-size, and pair-geometry flags.

### Presets

- **B2S-001:** The dialog must provide a Standard short-read DNA preset with
  `FR`, `minins=0`, `maxins=500`, end-to-end alignment, mixed alignments
  allowed, discordant alignments allowed, and best-only reporting.
- **B2S-002:** The dialog must provide a Long-insert paired-end preset with
  `FR`, `minins=500`, `maxins=20000`, end-to-end alignment, mixed alignments
  allowed, and best-only reporting.
- **B2S-003:** The dialog must provide a Mate-pair preset with `RF`,
  `minins=500`, `maxins=20000`, end-to-end alignment, mixed alignments
  allowed, and best-only reporting.
- **B2S-004:** The dialog must provide an Adapter-contaminated / partial
  reference preset that uses the standard values plus local alignment.
- **B2S-005:** The dialog must provide a Structural-variant discovery preset
  that uses the standard values plus dovetailing, while leaving mixed and
  discordant alignments allowed.
- **B2S-006:** The dialog must provide a Repeat / multi-mapping analysis
  preset that uses the standard values plus `report_k=10`.
- **B2S-007:** Every preset must display a short description stating that
  insert-size values are starting points and should be checked against the
  library.
- **B2S-008:** Presets must be selected explicitly by the user and must not be
  inferred from organism or assay metadata.

### UI and provenance

- **B2U-001:** The dialog must open with Standard short-read DNA selected when
  no other parameter values are supplied.
- **B2U-002:** Selecting a preset must apply its complete parameter bundle.
- **B2U-003:** Editing a preset-controlled field must change the selector to
  Custom.
- **B2U-004:** The user must be able to select a preset again after editing,
  replacing the edited values with the preset bundle.
- **B2U-005:** Pair-only fields must be disabled or hidden for single-end
  input.
- **B2U-006:** The UI must show an inline error when `minins` exceeds
  `maxins`.
- **B2U-007:** The UI must show an inline error when bounded and unbounded
  multi-mapping are selected together.
- **B2U-008:** The resolved parameter values, selected preset, and subsequent
  user edits must be included in the run's recorded provenance.

## Proposed architecture

The change extends the existing per-aligner model rather than creating a
Bowtie2-specific form or preset mechanism.

`backend/app/pipelines/align_params.py` will own the Bowtie2 dataclass,
normalization, and validation. `backend/app/pipelines/aligner_registry.py`
will own the field metadata and named preset bundles. The generated parameter
form will render the new metadata without a bespoke Bowtie2 component.
`backend/app/pipelines/align_runner.py` will translate the validated model to
Bowtie2 flags. The frontend type declaration will mirror the new open
parameter keys.

The data flow is:

```text
Preset selection
    -> dialog stores parameter overrides
    -> API receives params
    -> Bowtie2Params validates and normalizes them
    -> align_runner emits the command
    -> sanitized resolved values are recorded in provenance
```

The backend remains authoritative. Client-side validation exists for immediate
feedback but cannot replace server-side validation.

## UI organization

The generated fields should remain grouped as follows:

- Existing sensitivity and local alignment controls: biology/alignment mode.
- `minins`, `maxins`, orientation, dovetail, contain, and overlap controls:
  paired-end geometry.
- `report_k` and `report_all`: reporting/multi-mapping.

The preset selector should appear before these groups, with the description
directly below it. A user who changes any preset-controlled value enters Custom
mode; selecting a named preset again replaces those edits deliberately.

## Error handling

Validation errors must identify the offending parameter and correction:

- `minins` must be non-negative and no greater than `maxins`.
- `maxins` must be at least one.
- Orientation must be `FR`, `RF`, or `FF`.
- `report_k` must be non-negative.
- `report_k > 0` and `report_all=true` are mutually exclusive.
- Unknown preset IDs must be rejected rather than silently falling back.

Pair-only settings are not errors for a single-end launch; they are omitted
from the command because they have no meaning without mates.

## Testing strategy

Backend parameter tests must verify default compatibility, serialization of
every new field, invalid-value rejection, and preset bundle resolution.

Command-construction tests must verify each new flag, omission of default or
disabled flags where specified, omission of all pair-only flags for single-end
input, and mutual exclusion of `-k` and `-a`.

Frontend tests or existing dialog verification must cover preset application,
transition to Custom after editing, replacement when a preset is selected
again, and the two inline validation states. The generated-field path should
be used; no Bowtie2-specific form test should be needed unless the shared
renderer cannot express the new behavior.

A compact command-matrix test should exercise the six presets and compare the
resolved parameter values and resulting Bowtie2 arguments. This is the guard
against drift between registry metadata, validation, and command construction.

## Decision record

- **Decision:** Use workflow/read-characteristic presets rather than organism
  presets.
- **Reason:** Bowtie2 behavior depends on library geometry, read treatment,
  mate orientation, and reporting needs; organism category is not a reliable
  proxy for those properties.
- **Decision:** Add core paired-end and reporting controls, but defer the full
  scoring/seed-search surface.
- **Reason:** The selected controls address common workflow differences while
  avoiding a form that encourages unsupported or poorly understood tuning.
- **Decision:** Keep defaults conservative and preserve current behavior.
- **Reason:** Existing launches must not change merely because the parameter
  model gains new controls.

## References

- GitHub issue: https://github.com/syntheticgio/bioflow/issues/567
- Bowtie 2 manual: https://bowtie-bio.sourceforge.net/bowtie2/manual.shtml
- Existing parameter model: `backend/app/pipelines/align_params.py`
- Existing registry and generated field metadata:
  `backend/app/pipelines/aligner_registry.py`
- Existing generated form: `frontend/src/components/AlignerParamFields.tsx`
