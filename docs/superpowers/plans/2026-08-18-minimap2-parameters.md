# Curated Minimap2 Parameters Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Add a curated, validated set of Minimap2 sensitivity, secondary-alignment, performance, and output controls without overriding the selected read-type preset when an advanced field is unset.

**Architecture:** Extend the existing `Minimap2Params` model and registry-driven `ParamField` schema. Represent new controls as optional overrides, pass only explicit values to the existing Minimap2 command builder, and keep the frontend generic by rendering the server schema through `AlignerParamFields`. Parameter sets continue deriving eligible keys from registry metadata while excluding unset optional values.

**Tech Stack:** Python dataclasses and pytest; FastAPI schema serialization; React/TypeScript with TanStack Query; Docker Compose for backend and manual UI verification.

## Global Constraints

- The selected Minimap2 `-x` preset remains the primary biological configuration.
- An unset optional field means Minimap2's preset default; it must not emit a fixed BioFlow override.
- New fields must be declared in `AlignerSpec.fields`; do not add a Minimap2-specific frontend form.
- Invalid values must fail before queueing with the existing `ValidationError` path.
- Parameter-set eligibility remains derived from `ParamField` metadata.
- Do not expose `-A`, `-B`, `-O`, `-E`, `-r`, `-z`, `-I`, low-level seeding controls, splice-specific options, or low-level memory-capacity flags in v1.
- Backend tests run with `./backend/run-worktree-tests.sh`; manual UI verification uses `./ops/worktree-up.sh` on port 5273.

---

## File map

- Modify `backend/app/pipelines/align_params.py`: typed Minimap2 overrides, optional serialization, and validation.
- Modify `backend/app/pipelines/aligner_registry.py`: field metadata, groups, help, bounds, choices, and nullable defaults.
- Modify `backend/app/pipelines/align_runner.py`: deterministic Minimap2 flag emission.
- Modify `backend/app/services/parameter_set_service.py`: omit unset optional values when saving sets.
- Modify `frontend/src/api/types/alignment.ts` and `frontend/src/components/AlignerParamFields.tsx`: frontend types and nullable/float rendering.
- Modify `frontend/src/components/AlignDialog.tsx` only for preset-default explanatory copy.
- Test in `backend/tests/pipelines/test_align_params.py`, `backend/tests/pipelines/test_align_runner.py`, `backend/tests/api/test_pipelines_align_schema.py`, and `backend/tests/services/test_parameter_set_service.py`.

## Task 1: Add Minimap2 model fields and validation

**Files:**
- Modify: `backend/app/pipelines/align_params.py`
- Test: `backend/tests/pipelines/test_align_params.py`

**Interfaces:**
- `Minimap2Params` gains optional `kmer_size`, `window_size`, `min_chain_score`, `max_gap`, `secondary_ratio`, `max_secondary`, `batch_size`, and optional boolean/select output controls `secondary_mode`, `soft_clip_supplementary`, `cs_mode`, and `emit_md`.
- `from_dict(data: dict) -> Minimap2Params` validates choices and numeric bounds.
- `as_dict() -> dict` omits unset Minimap2-only numeric overrides while retaining existing shared fields.

- [ ] Write failing tests for default unset values, a full round trip, and invalid lower bounds, invalid `secondary_ratio` values outside [0, 1], invalid `secondary_mode`, and invalid `cs_mode`.

Use this contract in the tests:

```python
params = align_params.from_dict({"aligner": "minimap2"})
assert params.kmer_size is None
assert params.secondary_mode is None
assert params.cs_mode is None
assert params.emit_md is None

original = align_params.from_dict({
    "aligner": "minimap2", "preset": "map-ont",
    "kmer_size": 19, "window_size": 10, "min_chain_score": 40,
    "max_gap": 5000, "secondary_ratio": 0.8, "max_secondary": 10,
    "secondary_mode": "disabled", "batch_size": 1000000,
    "soft_clip_supplementary": True, "cs_mode": "long", "emit_md": True,
})
assert align_params.from_dict(original.as_dict()) == original
```

- [ ] Run `./backend/run-worktree-tests.sh tests/pipelines/test_align_params.py -q` and verify the new tests fail.
- [ ] Implement the fields with `None` defaults for numeric and optional boolean overrides. Normalize the UI sentinel choices `default` and `none` to `None` in the model, retain choices `default/enabled/disabled` and `none/short/long` in the schema, and use explicit bounds and conditional numeric parsing.
- [ ] Run the same focused test command and verify it passes.
- [ ] Commit with `git commit -m "feat(pipelines): validate curated minimap2 parameters"`.

## Task 2: Publish registry metadata and render it generically

**Files:**
- Modify: `backend/app/pipelines/aligner_registry.py`
- Modify: `frontend/src/api/types/alignment.ts`
- Modify: `frontend/src/components/AlignerParamFields.tsx`
- Modify: `frontend/src/components/AlignDialog.tsx`
- Test: `backend/tests/api/test_pipelines_align_schema.py`

**Interfaces:**
- `schema_for(Aligner.MINIMAP2)` returns all new fields with stable keys, groups, defaults, bounds, choices, and non-empty help.
- `ParamField.kind` gains a shared `float` kind only if needed for `secondary_ratio`.
- Blank nullable numeric inputs become omitted overrides, not zero-valued overrides.

- [ ] Add a failing schema test asserting the keys `preset`, `kmer_size`, `window_size`, `min_chain_score`, `max_gap`, `secondary_ratio`, `max_secondary`, `secondary_mode`, `batch_size`, `soft_clip_supplementary`, `cs_mode`, and `emit_md`; assert correct kinds/groups and non-empty help.
- [ ] Run `./backend/run-worktree-tests.sh tests/api/test_pipelines_align_schema.py -q` and verify failure.
- [ ] Add the fields to the Minimap2 `AlignerSpec.fields` tuple. Group sensitivity fields under biology and batch/output controls under performance. Use nullable numeric/boolean defaults and choices matching Task 1.
- [ ] Add optional keys to `AlignParams` and render nullable int/float fields as empty inputs; convert empty input back to `undefined`. Map the `default`/`none` select sentinels back to `undefined`, and leave unchecked optional checkboxes undefined. Keep the generic schema-driven form and add only the copy needed to explain that blank uses the selected preset's default.
- [ ] Run `./backend/run-worktree-tests.sh tests/api/test_pipelines_align_schema.py -q` and `cd frontend && npm run build`.
- [ ] Commit with `git commit -m "feat(ui): expose curated minimap2 fields in the align dialog"`.

## Task 3: Emit deterministic Minimap2 flags

**Files:**
- Modify: `backend/app/pipelines/align_runner.py`
- Test: `backend/tests/pipelines/test_align_runner.py`

**Interfaces:**
- The existing Minimap2 command branch consumes `Minimap2Params` and preserves the base command `minimap2 -a -x <preset> -t <threads> -R <read-group> <reference> <reads>`.
- Explicit overrides are appended in a stable order.

- [ ] Add failing tests proving no advanced flags appear for unset fields and all configured fields appear once. Cover `-k`, `-w`, `-m`, `-g`, `-p`, `-N`, `--secondary=no`, `-K`, `-Y`, `--cs=long`, and `--MD`. Add separate tests for `secondary_mode="enabled"` and `cs_mode="short"`.
- [ ] Run `./backend/run-worktree-tests.sh tests/pipelines/test_align_runner.py -q` and verify failure.
- [ ] Append flags in this order: `-k`, `-w`, `-m`, `-g`; then `-p`, `-N`, and `--secondary=...`; then `-K`, `-Y`, `--cs[=long]`, and `--MD`; then existing read-group and paths.
- [ ] Emit nothing for unset numeric/boolean values, `secondary_mode=None`, or `cs_mode=None`; emit the selected secondary mode, `cs` mode, `-Y`, and `--MD` only when explicitly configured. Preserve all other aligners.
- [ ] Run `./backend/run-worktree-tests.sh tests/pipelines/test_align_runner.py tests/pipelines/test_align_params.py -q`.
- [ ] Commit with `git commit -m "feat(pipelines): pass curated minimap2 flags"`.

## Task 4: Preserve explicit-only values in parameter sets

**Files:**
- Modify: `backend/app/services/parameter_set_service.py`
- Test: `backend/tests/services/test_parameter_set_service.py`
- Inspect: `frontend/src/components/ParameterSetPicker.tsx`

**Interfaces:**
- `eligible_params(family, tool, params)` filters schema-declared keys and removes only `None` values.
- `resolve_params(family, tool, params)` continues returning existing drift rejection reasons.

- [ ] Add a failing test that `{"preset": "map-ont", "kmer_size": None, "window_size": 10, "emit_md": None}` becomes `{"preset": "map-ont", "window_size": 10}`.
- [ ] Run `./backend/run-worktree-tests.sh tests/services/test_parameter_set_service.py -q` and verify failure.
- [ ] Filter `None` after the existing schema-key intersection. Do not filter explicit `False`, `0`, or valid empty strings when those values are represented by a field that is not optional.
- [ ] Add resolution coverage for explicit values and invalid saved values.
- [ ] Run `./backend/run-worktree-tests.sh tests/services/test_parameter_set_service.py tests/api/test_parameter_sets.py -q`.
- [ ] Commit with `git commit -m "fix(pipelines): omit unset minimap2 overrides from presets"`.

## Task 5: Cross-layer verification and manual UI check

**Files:**
- Inspect: `docs/superpowers/specs/2026-08-18-minimap2-parameters-design.md`
- Modify only if manual verification identifies missing user-facing help.

- [ ] Run the focused suite:

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_align_params.py tests/pipelines/test_align_runner.py tests/api/test_pipelines_align_schema.py tests/services/test_parameter_set_service.py -q
```

- [ ] Start the isolated stack with `./ops/worktree-up.sh`. Verify port 5273 shows Minimap2 fields, blank fields remain preset defaults, changing presets does not populate overrides, and selected values become explicit.
- [ ] Launch representative short-read and long-read alignments, one with no overrides and one with selected overrides. Verify commands contain selected flags exactly once, omit unselected flags, and recorded provenance reflects explicit settings.
- [ ] Stop the isolated stack with `./ops/worktree-up.sh --down`.
- [ ] Run the full suite with `./backend/run-worktree-tests.sh tests/ -q` and record the reported passing count.
- [ ] If help text needs clarification, update the specific documentation file and commit separately with `git commit -m "docs(pipelines): explain minimap2 advanced parameters"`.
