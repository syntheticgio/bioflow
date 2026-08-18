# Schema-loading Bowtie2 preset fix report

## Status

Fixed. `reconcileParameterSetPreset()` now preserves the current Bowtie2 preset
while schema preset metadata is still unavailable, and only resolves
named-vs-custom once `schema.presets` exists.

## What changed

- `initialPresetSelection()` now keeps an existing Bowtie2 preset ID when the
  schema has not loaded yet, instead of collapsing to `null`.
- `reconcileParameterSetPreset()` now preserves the current Bowtie2 preset in
  both the returned selection and the outgoing overrides while preset metadata
  is unavailable.
- Added a focused regression test for applying a saved Bowtie2 set during the
  schema-loading window.
- Kept the divergent-values-to-custom regression in place.

## Verification

- Focused frontend test file:
  `npm test -- src/components/alignDialogPresets.test.ts`
- Frontend lint:
  `npm run lint`
- Frontend build:
  `npm run build`
- Diff hygiene:
  `git diff --check`

## Commit

`fix(frontend): preserve preset while schema loads`

## Concerns

- The frontend build still emits the existing Vite chunk-size warning for the
  main JS bundle.
