# Bowtie2 Parameters and Workflow Presets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Bowtie2 paired-end geometry, reporting controls, and explicit workflow/read-characteristic presets while preserving existing defaults.

**Architecture:** Extend `Bowtie2Params` as the authoritative validated model, add matching registry metadata and preset bundles, emit the validated values in `align_runner`, and update the generated alignment dialog to expose editable preset fields and pair-aware validation. Existing schema generation and provenance paths remain the integration points.

**Tech Stack:** Python dataclasses and pytest; FastAPI schema metadata; React/TypeScript with the existing generated `AlignerParamFields` renderer; Docker Compose for backend verification; browser verification at the worktree UI port when UI behavior changes.

## Global Constraints

- Preserve current Bowtie2 defaults: sensitivity `--sensitive`, end-to-end alignment, `maxins=500`, mixed and discordant alignments allowed, and best-only reporting.
- Use workflow/read-characteristic presets, not organism or taxonomy inference.
- Keep the backend authoritative; frontend validation is immediate feedback only.
- Pair-only flags must be omitted for single-end launches.
- Do not expose Bowtie2 scoring/seed-search controls such as `--score-min`, `--ma`, `--mp`, `--np`, `--rdg`, `--rfg`, `-D`, `-R`, `-N`, or `-L` in this change.
- Do not change Bowtie2 index construction.
- Run backend tests from the worktree with `./backend/run-worktree-tests.sh`; do not use the main-checkout `docker compose exec api` command from this worktree.

---

## File map

- Modify `backend/app/pipelines/align_params.py`: Bowtie2 fields, serialization, parsing, and validation.
- Modify `backend/app/pipelines/aligner_registry.py`: generated field metadata, six preset bundles, and preset descriptions.
- Modify `backend/app/pipelines/align_runner.py`: Bowtie2 argument emission for paired-end inputs.
- Modify `backend/tests/pipelines/test_align_params.py`: model defaults, serialization, and validation tests.
- Modify `backend/tests/pipelines/test_aligner_registry.py`: field-to-model and preset-schema coverage.
- Modify `backend/tests/pipelines/test_align_runner.py`: command-matrix coverage for paired and single-end Bowtie2 launches.
- Modify `frontend/src/api/types/alignment.ts`: new `AlignParams` keys and any preset-related typing comments.
- Modify `frontend/src/components/AlignDialog.tsx`: preset label, preset/custom state, editable fields, pair-aware field presentation, and inline validation.
- Modify `frontend/src/components/AlignerParamFields.tsx` only if the shared renderer needs a field-level error or disabled-state interface; do not create a Bowtie2-specific renderer.

## Interfaces between tasks

- `Bowtie2Params.from_dict(data: dict) -> Bowtie2Params` returns validated values for `minins`, `maxins`, `orientation`, `dovetail`, `no_contain`, `no_overlap`, `report_k`, and `report_all`, plus the existing fields.
- `Bowtie2Params.as_dict() -> dict` includes every Bowtie2-specific field so the resolved values can be recorded in run provenance.
- `aligner_registry.schema_for(Aligner.BOWTIE2)` returns field metadata whose keys match `Bowtie2Params` attributes and presets whose `values` use those same keys.
- The Bowtie2 branch of `align_runner` receives a validated `Bowtie2Params` object and emits flags based on `r2 is not None`.
- `AlignDialog` continues to pass field changes through `AlignerParamFields`; the dialog owns preset selection and cross-field UI validation.

## Task 1: Extend and validate the Bowtie2 parameter model

**Files:**
- Modify: `backend/app/pipelines/align_params.py:205-265`
- Test: `backend/tests/pipelines/test_align_params.py:49-67`

**Interfaces:**
- Consumes: existing `BaseAlignParams._shared()` and `ValidationError`.
- Produces: validated `Bowtie2Params` values used by the registry and runner.

- [ ] **Step 1: Write failing model tests**

Add tests in `TestBowtie2` for the defaults, serialization, accepted orientation values, minimum insert size, pair-geometry booleans, `report_all`, and invalid combinations:

```python
def test_new_fields_keep_bowtie2_defaults(self):
    p = align_params.from_dict({"aligner": "bowtie2"})
    assert p.minins == 0
    assert p.maxins == 500
    assert p.orientation == "FR"
    assert p.dovetail is False
    assert p.no_contain is False
    assert p.no_overlap is False
    assert p.report_k == 0
    assert p.report_all is False

def test_new_fields_round_trip_through_as_dict(self):
    p = align_params.from_dict({
        "aligner": "bowtie2",
        "minins": 500,
        "maxins": 20000,
        "orientation": "RF",
        "dovetail": True,
        "no_contain": True,
        "no_overlap": True,
        "report_all": True,
    })
    out = p.as_dict()
    assert out["minins"] == 500
    assert out["maxins"] == 20000
    assert out["orientation"] == "RF"
    assert out["dovetail"] is True
    assert out["no_contain"] is True
    assert out["no_overlap"] is True
    assert out["report_all"] is True

@pytest.mark.parametrize("orientation", ["FR", "RF", "FF"])
def test_orientation_accepts_documented_values(self, orientation):
    assert align_params.from_dict({"aligner": "bowtie2", "orientation": orientation}).orientation == orientation

def test_minins_must_not_exceed_maxins(self):
    with pytest.raises(ValidationError, match="minins.*maxins"):
        align_params.from_dict({"aligner": "bowtie2", "minins": 501, "maxins": 500})

def test_report_k_and_report_all_are_mutually_exclusive(self):
    with pytest.raises(ValidationError, match="report_k.*report_all"):
        align_params.from_dict({"aligner": "bowtie2", "report_k": 10, "report_all": True})
```

Also add parametrized rejection tests for `minins < 0`, `maxins < 1`, `report_k < 0`, and an unknown orientation.

- [ ] **Step 2: Run the focused tests and verify failure**

Run:

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_align_params.py -q
```

Expected: the new tests fail because the dataclass does not yet define the new attributes or validation.

- [ ] **Step 3: Implement the minimal model changes**

In `Bowtie2Params`:

1. Add `minins: int = 0`, `orientation: str = "FR"`, `dovetail: bool = False`, `no_contain: bool = False`, `no_overlap: bool = False`, and `report_all: bool = False` beside the existing Bowtie2 fields.
2. Add `minins`, `orientation`, `dovetail`, `no_contain`, `no_overlap`, and `report_all` to `as_dict()`.
3. Define a module-level `BOWTIE2_ORIENTATIONS = ("FR", "RF", "FF")`.
4. Parse integer and boolean values in `from_dict()` using the existing conventions.
5. Reject invalid ranges, orientations, and the `report_k > 0` plus `report_all` combination before constructing the dataclass.

Do not alter the existing `maxins=500`, sensitivity, local, mixed, or discordant defaults.

- [ ] **Step 4: Run the focused tests and verify success**

Run:

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_align_params.py -q
```

Expected: all tests in the file pass, including the pre-existing Bowtie2 and shared-parameter tests.

- [ ] **Step 5: Commit the model change**

```bash
git add backend/app/pipelines/align_params.py backend/tests/pipelines/test_align_params.py
git commit -m "feat(aligners): validate Bowtie2 paired-end parameters"
```

## Task 2: Add registry metadata and workflow presets

**Files:**
- Modify: `backend/app/pipelines/aligner_registry.py:383-478`
- Test: `backend/tests/pipelines/test_aligner_registry.py`

**Interfaces:**
- Consumes: `Bowtie2Params` attributes and `BOWTIE2_ORIENTATIONS` from Task 1.
- Produces: schema fields and preset bundles consumed by `AlignDialog` and parameter-set/provenance paths.

- [ ] **Step 1: Write failing registry tests**

Add tests that inspect `schema_for(Aligner.BOWTIE2)` and assert:

```python
def test_bowtie2_schema_declares_new_fields():
    schema = aligner_registry.schema_for(Aligner.BOWTIE2)
    fields = {field["key"]: field for field in schema["fields"]}
    assert fields["minins"]["kind"] == "int"
    assert fields["minins"]["default"] == 0
    assert fields["orientation"]["choices"] == [
        {"value": "FR", "label": "FR (paired-end)"},
        {"value": "RF", "label": "RF (mate-pair)"},
        {"value": "FF", "label": "FF"},
    ]
    assert {"dovetail", "no_contain", "no_overlap", "report_all"} <= fields.keys()

def test_bowtie2_schema_exposes_all_workflow_presets():
    schema = aligner_registry.schema_for(Aligner.BOWTIE2)
    assert set(schema["presets"]) == {
        "standard_short_read",
        "long_insert",
        "mate_pair",
        "adapter_partial_reference",
        "structural_variant",
        "repeat_multimapping",
    }

def test_every_bowtie2_preset_values_are_validated():
    schema = aligner_registry.schema_for(Aligner.BOWTIE2)
    for preset in schema["presets"].values():
        align_params.from_dict({"aligner": "bowtie2", **preset["values"]})
```

Add exact value assertions for each preset so a later edit cannot silently change the intended bundle.

- [ ] **Step 2: Run the registry tests and verify failure**

Run:

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_aligner_registry.py -q
```

Expected: the new tests fail because the Bowtie2 schema has neither the new fields nor the six presets.

- [ ] **Step 3: Add the field metadata**

In the Bowtie2 `fields` tuple, add:

- `minins`: integer, default `0`, minimum `0`, grouped with paired-end geometry.
- `maxins`: retain the existing integer field, update help text to explain the `-I`/`-X` range.
- `orientation`: select with `FR`, `RF`, and `FF` choices, default `FR`.
- `dovetail`, `no_contain`, and `no_overlap`: booleans with concise explanations of their pair-geometry effect.
- `report_all`: boolean explaining that `-a` can substantially increase output and is mutually exclusive with `-k`.

Keep the existing sensitivity, local, mixed, discordant, shared, and report-k metadata. Use the existing `biology` group unless the renderer is extended to support a new group; do not create a Bowtie2-specific renderer.

- [ ] **Step 4: Add the six complete preset bundles**

Add `presets` to the Bowtie2 `AlignerSpec` with these values:

```python
"standard_short_read": {
    "label": "Standard short-read DNA",
    "description": "Conservative paired-end defaults; check insert sizes against the library.",
    "values": {
        "sensitivity": "--sensitive", "local": False,
        "minins": 0, "maxins": 500, "orientation": "FR",
        "no_mixed": False, "no_discordant": False,
        "dovetail": False, "no_contain": False, "no_overlap": False,
        "report_k": 0, "report_all": False,
    },
},
"long_insert": {
    "label": "Long-insert paired-end",
    "description": "Broad starting range for long-insert libraries; check the library distribution.",
    "values": {"minins": 500, "maxins": 20000, "orientation": "FR", "report_k": 0, "report_all": False},
},
"mate_pair": {
    "label": "Mate-pair",
    "description": "RF mate-pair starting values; confirm orientation and insert range for the protocol.",
    "values": {"minins": 500, "maxins": 20000, "orientation": "RF", "report_k": 0, "report_all": False},
},
"adapter_partial_reference": {
    "label": "Adapter-contaminated / partial reference",
    "description": "Uses local alignment to tolerate unaligned read ends or a partial reference.",
    "values": {"local": True, "minins": 0, "maxins": 500, "orientation": "FR", "report_k": 0, "report_all": False},
},
"structural_variant": {
    "label": "Structural-variant discovery",
    "description": "Preserves discordant and mixed evidence and allows dovetailing mates.",
    "values": {"dovetail": True, "minins": 0, "maxins": 500, "orientation": "FR", "report_k": 0, "report_all": False},
},
"repeat_multimapping": {
    "label": "Repeat / multi-mapping analysis",
    "description": "Reports up to 10 alignments per read; output size can grow substantially.",
    "values": {"minins": 0, "maxins": 500, "orientation": "FR", "report_k": 10, "report_all": False},
},
```

Include the full values required to make each preset deterministic, not only the values that differ from the standard preset. Keep `report_all=False` in every named preset so `-a` remains an explicit advanced choice.

- [ ] **Step 5: Run registry and schema tests**

Run:

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_aligner_registry.py tests/api/test_pipelines_align_schema.py -q
```

Expected: all field metadata, schema serialization, and preset validation tests pass.

- [ ] **Step 6: Commit the registry change**

```bash
git add backend/app/pipelines/aligner_registry.py backend/tests/pipelines/test_aligner_registry.py
git commit -m "feat(aligners): add Bowtie2 workflow presets"
```

## Task 3: Emit Bowtie2 flags and test command construction

**Files:**
- Modify: `backend/app/pipelines/align_runner.py:620-653`
- Test: `backend/tests/pipelines/test_align_runner.py`

**Interfaces:**
- Consumes: validated `Bowtie2Params` from Task 1 and the paired/single input distinction already represented by `r2: Path | None`.
- Produces: a Bowtie2 argv list with pair-only flags gated on paired input.

- [ ] **Step 1: Write failing command tests**

Add focused tests using the existing runner test fixtures/helpers. Assert the paired command contains the new flags:

```python
def test_bowtie2_paired_command_emits_geometry_and_reporting_flags():
    params = Bowtie2Params.from_dict({
        "aligner": "bowtie2", "minins": 500, "maxins": 20000,
        "orientation": "RF", "dovetail": True,
        "no_contain": True, "no_overlap": True, "report_k": 10,
    })
    argv = build_align_argv(Aligner.BOWTIE2, "bowtie2", reference, r1, r2, read_group, params)
    assert argv[argv.index("-I") + 1] == "500"
    assert argv[argv.index("-X") + 1] == "20000"
    assert "--rf" in argv
    assert {"--dovetail", "--no-contain", "--no-overlap"} <= set(argv)
    assert argv[argv.index("-k") + 1] == "10"
    assert "-a" not in argv

def test_bowtie2_single_end_omits_pair_only_flags():
    params = Bowtie2Params.from_dict({
        "aligner": "bowtie2", "minins": 500, "maxins": 20000,
        "orientation": "RF", "dovetail": True,
    })
    argv = build_align_argv(Aligner.BOWTIE2, "bowtie2", reference, r1, None, read_group, params)
    assert "-I" not in argv and "-X" not in argv
    assert not {"--fr", "--rf", "--ff", "--dovetail", "--no-contain", "--no-overlap"} & set(argv)
```

Add separate assertions that `report_all=True` emits `-a` and that `report_k=0` emits neither `-k` nor `-a`.

- [ ] **Step 2: Run the focused runner tests and verify failure**

Run:

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_align_runner.py -q
```

Expected: the new tests fail because the runner currently always emits `-X` and does not emit the new controls.

- [ ] **Step 3: Implement pair-aware Bowtie2 argument construction**

In the Bowtie2 branch of `build_align_argv`:

1. Keep sensitivity and local handling unchanged.
2. Wrap all insert-size, orientation, and pair-geometry emission in `if r2 is not None:`.
3. Emit `-I` only when `params.minins > 0`.
4. Emit `-X <maxins>` for paired input using the validated value.
5. Map `FR`, `RF`, and `FF` to `--fr`, `--rf`, and `--ff`.
6. Append enabled `--dovetail`, `--no-contain`, and `--no-overlap` flags.
7. Keep `-k` handling mutually exclusive with `-a`; emit `-a` only when `params.report_all` is true.

Do not add pair flags to the shared HISAT2 path. Do not pass `-I`, `-X`, orientation, or pair geometry to single-end Bowtie2.

- [ ] **Step 4: Run all alignment runner tests**

Run:

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_align_runner.py tests/pipelines/test_align_launch.py -q
```

Expected: all existing and new command tests pass.

- [ ] **Step 5: Commit the runner change**

```bash
git add backend/app/pipelines/align_runner.py backend/tests/pipelines/test_align_runner.py
git commit -m "feat(aligners): emit Bowtie2 pair geometry flags"
```

## Task 4: Update the generated alignment dialog

**Files:**
- Modify: `frontend/src/api/types/alignment.ts:24-55`
- Modify: `frontend/src/components/AlignDialog.tsx:639-724`
- Modify: `frontend/src/components/AlignerParamFields.tsx` only if the existing renderer cannot accept the required disabled/error props.

**Interfaces:**
- Consumes: the Bowtie2 schema and presets from Task 2, plus validated parameter keys from Task 1.
- Produces: an explicit preset selector with editable/custom behavior and immediate cross-field feedback.

- [ ] **Step 1: Update TypeScript parameter types**

Add these optional properties to `AlignParams`:

```ts
minins?: number;
orientation?: "FR" | "RF" | "FF";
dovetail?: boolean;
no_contain?: boolean;
no_overlap?: boolean;
report_all?: boolean;
```

Keep `maxins`, `report_k`, `preset`, and the other aligner-specific fields unchanged.

- [ ] **Step 2: Make preset state explicit and user-readable**

In `AlignDialog`:

1. Change the selector label from `Organism preset` to `Alignment preset`.
2. Keep the current selected preset in state, but initialize it from `params.preset` when the server/card supplies one.
3. Ensure the selector has a stable Standard short-read DNA default for Bowtie2 when no preset is supplied; this must not mutate unrelated aligners.
4. When a named preset is selected, merge its complete values into `overrides` and record its ID in `preset`.
5. When a field controlled by the active named preset changes, set the selector to `Custom` and clear the stored preset ID while retaining the edited values.
6. Preserve the existing Advanced behavior for aligners that use it, but present the Bowtie2 named presets as editable rather than hiding all individual Bowtie2 fields.

The implementation must not add a second preset system; `ParameterSetPicker` remains the saved-user-parameter-set path.

- [ ] **Step 3: Render Bowtie2 fields with pair-aware visibility and feedback**

Use the generated `AlignerParamFields` metadata. For Bowtie2, show the new fields in the existing biology section when the user is in Custom/Advanced mode or while editing a named preset. Pair-only controls must be disabled or hidden when `paired`/`usePair` is false.

Add inline messages in the dialog for:

```ts
const insertRangeError = params.minins != null && params.maxins != null && params.minins > params.maxins;
const reportingError = Boolean(params.report_all) && Number(params.report_k ?? 0) > 0;
```

The Launch action must remain blocked while either error is true, and the message must identify the correction. The backend still rejects the same combinations.

If `AlignerParamFields` needs changes, add only generic props such as a disabled-key predicate or field-error map; do not add a Bowtie2 branch to the renderer.

- [ ] **Step 4: Run frontend type/build checks**

Run:

```bash
cd frontend && npm run lint
```

Expected: the TypeScript compiler exits successfully with no errors. Run `npm run build` as the follow-up if the lint/typecheck pass succeeds and the implementation changes Vite-facing code.

- [ ] **Step 5: Commit the dialog change**

```bash
git add frontend/src/api/types/alignment.ts frontend/src/components/AlignDialog.tsx frontend/src/components/AlignerParamFields.tsx
git commit -m "feat(frontend): expose Bowtie2 workflow parameters"
```

## Task 5: End-to-end verification and documentation review

**Files:**
- Modify: none expected; update an existing user-facing help surface only if the implementation reveals that the alignment help text does not describe the new presets.
- Test: existing backend suite and manual browser verification.

**Interfaces:**
- Consumes: all implementation tasks.
- Produces: evidence that schema, UI, validation, command construction, and provenance agree.

- [ ] **Step 1: Run the focused backend suite**

Run:

```bash
./backend/run-worktree-tests.sh \
  tests/pipelines/test_align_params.py \
  tests/pipelines/test_aligner_registry.py \
  tests/pipelines/test_align_runner.py \
  tests/pipelines/test_align_launch.py \
  tests/api/test_pipelines_align_schema.py -q
```

Expected: all focused tests pass.

- [ ] **Step 2: Verify the worktree UI manually**

Start the isolated stack from the worktree:

```bash
./ops/worktree-up.sh
```

Open `http://localhost:5273` and verify:

1. Bowtie2 shows `Alignment preset`, not `Organism preset`.
2. All six presets appear with descriptions.
3. Standard short-read DNA is the default for a fresh Bowtie2 dialog.
4. Selecting each preset changes the visible values to the documented bundle.
5. Editing a preset-controlled value changes the selector to Custom.
6. Single-end input hides or disables insert-size, orientation, dovetail, contain, and overlap controls.
7. Invalid insert ranges and simultaneous `-k`/`-a` show inline errors and block launch.
8. A valid paired-end Bowtie2 launch records the resolved values in provenance.

- [ ] **Step 3: Run the broader backend suite**

Run:

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: the full worktree backend suite passes. Record the reported pass/fail count rather than relying only on the process exit code.

- [ ] **Step 4: Stop the isolated stack**

```bash
./ops/worktree-up.sh --down
```

Expected: the worktree-only containers and volumes are removed; the shared main stack is untouched.

- [ ] **Step 5: Review the final diff and commit verification-only documentation if needed**

Run:

```bash
git diff --check
git status --short
git diff origin/main...HEAD --stat
```

If user-facing help text was required by the manual verification, commit that documentation separately with a `docs:` subject. Otherwise leave the code commits as the complete implementation history.

## Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-18-bowtie2-parameters.md`. Use `superpowers:subagent-driven-development` for fresh-agent task execution with review checkpoints, or `superpowers:executing-plans` for inline execution in this session.
