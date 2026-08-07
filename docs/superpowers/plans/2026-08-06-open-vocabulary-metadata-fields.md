# Open-Vocabulary Metadata Fields Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the eight metadata fields whose vocabularies come from outside this repo be typed into freely, so the 53 spurious "not one of the suggested options" warnings stop and users can enter the instrument models SRA already writes.

**Architecture:** A new `FieldDef.open_vocabulary` flag marks those fields. The backend suppresses the off-list warning for them; the frontend renders them as a `<input list=...>` + `<datalist>` combo instead of a `<select>`. No migration, no stored value changes meaning, and no change to `sam_platform()` or its read paths.

**Tech Stack:** Python 3.12 / FastAPI / pytest (backend), React + TypeScript (frontend), Docker Compose.

**Spec:** [`docs/superpowers/specs/2026-08-06-open-vocabulary-metadata-fields-design.md`](../specs/2026-08-06-open-vocabulary-metadata-fields-design.md)

---

## Before you start

**Baseline suite state, measured 2026-08-06 on this branch with a clean tree:**

```
1 failed, 3497 passed, 20 warnings in 116.32s
FAILED tests/services/test_provenance_verbs.py::test_every_registered_handler_is_classified
```

That one failure is **pre-existing and unrelated** to this work — it is a
handler-classification registry in a file no task here touches. Do not try to
fix it as part of this plan, and do not treat it as caused by your changes.
Your target on every run below is **that same single failure and no other**.
If a second failure appears, it is yours.

**Run tests from the worktree with:**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Never `docker compose exec api python -m pytest` — from a worktree that silently
tests `main`'s code, not yours (see `CLAUDE.md`, "Verifying changes"). The full
suite takes ~2 minutes; single-file runs are much faster and are what most steps
below use.

**Line numbers** in this plan were read on 2026-08-06. If a file has shifted,
search for the quoted code rather than trusting the number.

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `backend/app/metadata/schemas.py` | Modify | Add `open_vocabulary` to `FieldDef`, set it on 8 fields, drop `"Other"` from their options, make the warning conditional, serialize the flag |
| `backend/tests/storage/test_metadata_schemas.py` | Modify | Coercion/serialization tests live here; one existing test must be retargeted |
| `backend/tests/metadata/test_schemas_open_vocabulary.py` | Create | The open/closed partition tests — new concern, own file, matching `test_schemas_roles.py`'s "account for every member" style |
| `frontend/src/api/types.ts` | Modify | `MetadataField` gains `open_vocabulary: boolean` |
| `frontend/src/components/SchemaMetadataEditor.tsx` | Modify | Three-way branch: combo for open enums, `<select>` for closed |

Tasks 1–4 are backend and independently committable. Task 5 is frontend. Task 6
is the real-database check that the unit tests structurally cannot do.

---

### Task 1: Add the `open_vocabulary` flag to `FieldDef`

Nothing reads it yet. This task only makes it exist and reach the API.

**Files:**
- Modify: `backend/app/metadata/schemas.py:28-52`
- Test: `backend/tests/storage/test_metadata_schemas.py`

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/storage/test_metadata_schemas.py`, inside the existing
`class TestApiShape`:

```python
    def test_open_vocabulary_reaches_the_api(self):
        """The frontend picks its widget from this flag, so a field that is
        open on the backend and closed on the wire renders as a <select>
        and silently blocks the values SRA writes."""
        out = schemas.schema_for_api(FormatKind.FASTQ)
        flat = {f["key"]: f for g in out["groups"] for f in g["fields"]}
        assert flat["platform"]["open_vocabulary"] is True
        assert flat["read_type"]["open_vocabulary"] is False
```

- [ ] **Step 2: Run it to confirm it fails**

```bash
./backend/run-worktree-tests.sh tests/storage/test_metadata_schemas.py -q
```

Expected: FAIL with `KeyError: 'open_vocabulary'`.

- [ ] **Step 3: Add the field and serialize it**

In `backend/app/metadata/schemas.py`, add to `FieldDef` immediately after the
`suggested: bool = False` line and its comment:

```python
    # True when the values come from outside this repo -- NCBI, an instrument
    # vendor, a lab's own kit names -- so `options` is a set of suggestions
    # that will never be complete. The UI renders these as a free-text combo
    # rather than a <select>, and an off-list value is not a warning.
    #
    # Inclusion rule: open if the vocabulary is owned elsewhere; closed if this
    # repo or a published spec defines the complete set. Deliberately a
    # hand-maintained per-field flag and deliberately without an exhaustiveness
    # test -- see the spec's note on CLAUDE.md's three-way registry split. This
    # is the middle case, where forcing coverage would make a detector guess.
    open_vocabulary: bool = False
```

Then add to the `to_dict()` return dict, after `"suggested": self.suggested,`:

```python
            "open_vocabulary": self.open_vocabulary,
```

- [ ] **Step 4: Set the flag on `platform` only**

Enough to make the test pass; the other seven come in Task 2. In the
`FieldDef` for `platform` (around `schemas.py:185`), add `open_vocabulary=True,`
after `suggested=True,`.

- [ ] **Step 5: Run the test to confirm it passes**

```bash
./backend/run-worktree-tests.sh tests/storage/test_metadata_schemas.py -q
```

Expected: PASS, whole file green.

- [ ] **Step 6: Commit**

```bash
git add backend/app/metadata/schemas.py backend/tests/storage/test_metadata_schemas.py
git commit -m "feat(metadata): add FieldDef.open_vocabulary flag (#66)"
```

---

### Task 2: Flag the remaining seven fields and drop the `"Other"` sentinel

**Files:**
- Modify: `backend/app/metadata/schemas.py` (8 `FieldDef`s)
- Test: `backend/tests/metadata/test_schemas_open_vocabulary.py` (create)

The eight open fields, with the line each `FieldDef` starts near:

| Field | ~Line | Note |
|---|---|---|
| `organism` | 90 | |
| `assay` | 124 | |
| `library_prep` | 165 | |
| `platform` | 185 | already flagged in Task 1 |
| `reference_build` (alignment) | 199 | ENUM copy |
| `aligner` | 212 | |
| `reference_build` (variant) | 230 | the **second** ENUM copy — easy to miss |
| `variant_caller` | 241 | |
| `interval_type` | 319 | |

`reference_build` is defined **four** times in this file. Lines 265 and 317 are
plain `FieldType.TEXT` fields with no options (reference and intervals roles);
they are already open and must be left alone. Only the two ENUM copies — ~199
and ~230 — get the flag.

Verified 2026-08-06 against the running schema: 15 distinct ENUM `FieldDef`
objects across 14 keys, `reference_build` being the only key defined twice as
an ENUM. Note that iterating `FORMAT_FIELDS` and `ROLE_FIELDS` yields the same
objects repeatedly (the tuples are shared across formats and roles), so
`reference_build` appears five times and `aligner` three by that measure —
which is why the test helper dedupes by `id()`, not by key.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/metadata/test_schemas_open_vocabulary.py`:

```python
"""Every ENUM field is deliberately either open or closed.

An open field's options are suggestions from a vocabulary someone else owns
(NCBI, an instrument vendor, a lab's kit names). A closed field's options are
the complete set, defined by this repo or a published spec. The distinction
drives both the widget and whether an off-list value warns, so a field in
neither camp -- or both -- is a bug.
"""

from app.metadata import schemas
from app.metadata.schemas import FieldType

# Vocabularies this repo or a spec defines completely. Kept as a literal list
# rather than derived, so that adding an ENUM field forces a decision here
# instead of defaulting into one.
CLOSED_ENUM_FIELDS = {
    "sequence_type",   # derived from the SequenceType enum
    "sex",
    "read_type",
    "mate",
    "variant_type",
    "assembly_level",  # NCBI's fixed four
}

OPEN_ENUM_FIELDS = {
    "organism",
    "assay",
    "library_prep",
    "platform",
    "reference_build",
    "aligner",
    "variant_caller",
    "interval_type",
}


def _all_enum_fields():
    """Every ENUM FieldDef defined anywhere in the module.

    Deliberately walks the source tuples rather than `all_known_fields()`.
    That helper builds a dict keyed by field key using `setdefault`, so it
    keeps only the *first* definition per key and silently drops the rest --
    and `reference_build` is defined twice as an ENUM. Checked 2026-08-06: the
    alignment copy happens to win, so a test built on that helper would pass
    while never once looking at the variant copy. Silent-skip, not a test.

    Deduplicated by object identity, not by key: the field tuples are shared
    across several FORMAT_FIELDS/ROLE_FIELDS entries, so a plain walk yields
    the same object many times (`reference_build` five times, `aligner`
    three). Keying by `f.key` instead would collapse the two genuinely
    distinct `reference_build` definitions back into one.
    """
    seen: dict[int, schemas.FieldDef] = {}
    for group in (
        schemas.COMMON_FIELDS,
        *schemas.FORMAT_FIELDS.values(),
        *schemas.ROLE_FIELDS.values(),
    ):
        for field in group:
            if field.type is FieldType.ENUM:
                seen.setdefault(id(field), field)
    return list(seen.values())


def test_the_helper_sees_both_reference_build_enums():
    """Guards the docstring above. Measured 2026-08-06: 15 distinct ENUM
    FieldDef objects over 14 keys, `reference_build` being the only key with
    two. If this collection ever starts deduplicating by key, every other
    test in this file quietly narrows and none of them fails."""
    fields = _all_enum_fields()
    keys = [f.key for f in fields]
    assert keys.count("reference_build") == 2, (
        f"expected both ENUM copies, got {keys.count('reference_build')}"
    )
    assert len(fields) == 15, (
        f"expected 15 distinct ENUM FieldDefs, got {len(fields)}. If you added "
        "one, add it to OPEN_ENUM_FIELDS or CLOSED_ENUM_FIELDS and update this "
        "number."
    )


class TestOpenClosedPartition:
    def test_every_enum_field_is_open_or_closed(self):
        for field in _all_enum_fields():
            in_open = field.key in OPEN_ENUM_FIELDS
            in_closed = field.key in CLOSED_ENUM_FIELDS
            assert in_open or in_closed, (
                f"{field.key} is an ENUM in neither OPEN_ENUM_FIELDS nor "
                "CLOSED_ENUM_FIELDS. Decide whether its vocabulary is owned "
                "by this repo or by someone else."
            )

    def test_no_enum_field_is_both(self):
        overlap = OPEN_ENUM_FIELDS & CLOSED_ENUM_FIELDS
        assert not overlap, f"contradictory: {overlap}"

    def test_open_fields_carry_the_flag(self):
        for field in _all_enum_fields():
            if field.key in OPEN_ENUM_FIELDS:
                assert field.open_vocabulary is True, (
                    f"{field.key} is listed open but its FieldDef does not "
                    "set open_vocabulary=True"
                )

    def test_closed_fields_do_not_carry_the_flag(self):
        for field in _all_enum_fields():
            if field.key in CLOSED_ENUM_FIELDS:
                assert field.open_vocabulary is False, (
                    f"{field.key} is listed closed but sets open_vocabulary"
                )


class TestOtherSentinelIsGone:
    def test_no_open_field_offers_other(self):
        """With free text available, storing the literal 'Other' is strictly
        worse than storing the real answer."""
        for field in _all_enum_fields():
            if field.key in OPEN_ENUM_FIELDS:
                assert "Other" not in field.options, (
                    f"{field.key} still offers 'Other' as a selectable value"
                )

    def test_open_fields_still_offer_suggestions(self):
        """Dropping 'Other' must not empty a list -- the suggestions are the
        entire point of a combo over a plain text box."""
        for field in _all_enum_fields():
            if field.key in OPEN_ENUM_FIELDS:
                assert field.options, f"{field.key} has no suggestions left"
```

- [ ] **Step 2: Run it to confirm it fails**

```bash
./backend/run-worktree-tests.sh tests/metadata/test_schemas_open_vocabulary.py -q
```

Expected: FAIL — several fields listed open do not yet set the flag, and
several still offer `"Other"`.

If it instead fails inside `_all_enum_fields` with an `AttributeError`, check
what `schemas.all_known_fields()` returns: it maps key → `FieldDef`, so
`.values()` is right, but confirm rather than assume.

- [ ] **Step 3: Set the flag on the seven remaining fields**

For each of `organism`, `assay`, `library_prep`, `reference_build` (**both**
ENUM copies, ~199 and ~230), `aligner`, `variant_caller`, `interval_type`: add
`open_vocabulary=True,` alongside the existing keyword arguments.

- [ ] **Step 4: Remove `"Other"` from those eight option tuples**

Delete the `"Other"` entry from each. For example `library_prep` becomes:

```python
        options=("TruSeq", "Nextera", "NEBNext", "KAPA", "SMART-seq", "10x"),
```

and `aligner` becomes:

```python
        options=("BWA-MEM", "BWA-MEM2", "Bowtie2", "STAR", "HISAT2", "minimap2",
                 "DRAGEN"),
```

Do the same for `organism`, `assay`, `platform`, both `reference_build` ENUM
copies, `variant_caller`, and `interval_type`. Leave every other list untouched.

- [ ] **Step 5: Run the new test file**

```bash
./backend/run-worktree-tests.sh tests/metadata/test_schemas_open_vocabulary.py -q
```

Expected: PASS, 6 tests.

- [ ] **Step 6: Run the full suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: the pre-existing `test_every_registered_handler_is_classified`
failure, **plus** a new failure in
`tests/storage/test_metadata_schemas.py::TestValidationIsAdvisory::test_enum_value_outside_the_options_is_kept`.

That second failure is **expected and correct** — that test uses `aligner`,
which is now open. Task 3 fixes it deliberately. Do not fix it here and do not
delete it.

- [ ] **Step 7: Commit**

```bash
git add backend/app/metadata/schemas.py backend/tests/metadata/test_schemas_open_vocabulary.py
git commit -m "feat(metadata): mark the eight externally-owned vocabularies open (#66)"
```

---

### Task 3: Suppress the off-list warning for open fields

**Files:**
- Modify: `backend/app/metadata/schemas.py:548-556` (the ENUM branch of `_coerce`)
- Test: `backend/tests/storage/test_metadata_schemas.py`

- [ ] **Step 1: Retarget the existing test that Task 2 broke**

In `backend/tests/storage/test_metadata_schemas.py`, replace
`test_enum_value_outside_the_options_is_kept` with a version pointed at a
**closed** field. Its point — that a value is kept — is still true and still
worth testing; only the field it used stopped being closed.

```python
    def test_enum_value_outside_a_closed_field_is_kept_with_a_warning(self):
        """Lab vocabularies always outgrow a fixed list, so the value is kept.

        Uses read_type, a closed vocabulary: single/paired is the complete
        set, so an off-list value here really is worth flagging. This test
        used `aligner` until #66 made that an open vocabulary -- see
        test_open_vocabulary_value_is_not_a_warning below for that direction.
        """
        r = schemas.coerce_and_validate({"read_type": "triple-end"}, FormatKind.FASTQ)
        assert r.values["read_type"] == "triple-end"
        assert any("not one of the suggested" in w["message"] for w in r.warnings)
```

- [ ] **Step 2: Write the failing test for the new behaviour**

Add directly below it, in the same class:

```python
    def test_open_vocabulary_value_is_not_a_warning(self):
        """The defect from #66: every SRA-enriched file carried a warning
        that was wrong about which value was the authoritative one."""
        r = schemas.coerce_and_validate({"platform": "NextSeq 550"}, FormatKind.FASTQ)
        assert r.values["platform"] == "NextSeq 550"
        assert r.warnings == []

    def test_open_vocabulary_suppression_is_per_field(self):
        """One metadata dict, one open and one closed field, so a blanket
        suppression that ignores the flag fails here."""
        r = schemas.coerce_and_validate(
            {"platform": "NextSeq 550", "read_type": "triple-end"},
            FormatKind.FASTQ,
        )
        keys = {w["key"] for w in r.warnings}
        assert keys == {"read_type"}
```

- [ ] **Step 3: Run to confirm the new tests fail**

```bash
./backend/run-worktree-tests.sh tests/storage/test_metadata_schemas.py -q
```

Expected: `test_open_vocabulary_value_is_not_a_warning` and
`test_open_vocabulary_suppression_is_per_field` FAIL (a warning is produced);
`test_enum_value_outside_a_closed_field_is_kept_with_a_warning` PASSES already.

- [ ] **Step 4: Make the warning conditional**

In `_coerce`, change the ENUM branch from:

```python
        if spec.type is FieldType.ENUM:
            s = str(raw).strip()
            if spec.options and s not in spec.options:
                # Kept: lab vocabularies always outgrow a fixed list.
                return s, (
                    f"{spec.label}: {s!r} is not one of the suggested options; "
                    "stored anyway"
                )
            return s, None
```

to:

```python
        if spec.type is FieldType.ENUM:
            s = str(raw).strip()
            # An open field's options are suggestions from a vocabulary this
            # repo does not own, so an off-list value is the normal case rather
            # than a mistake -- SRA writes instrument models the dropdown never
            # listed. Warning on those was wrong about which value was
            # authoritative. A closed field's list really is complete, so an
            # off-list value there still earns the warning.
            if spec.options and not spec.open_vocabulary and s not in spec.options:
                # Kept regardless: lab vocabularies always outgrow a fixed list.
                return s, (
                    f"{spec.label}: {s!r} is not one of the suggested options; "
                    "stored anyway"
                )
            return s, None
```

- [ ] **Step 5: Run the file**

```bash
./backend/run-worktree-tests.sh tests/storage/test_metadata_schemas.py -q
```

Expected: PASS, whole file green.

- [ ] **Step 6: Run the full suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: **1 failed, N passed** — only the pre-existing
`test_every_registered_handler_is_classified`. The count should be roughly
3503 passed (3497 baseline + 6 new). If anything else fails, it is yours.

- [ ] **Step 7: Commit**

```bash
git add backend/app/metadata/schemas.py backend/tests/storage/test_metadata_schemas.py
git commit -m "fix(metadata): stop warning on values from open vocabularies (#66)"
```

---

### Task 4: Carry the flag in the frontend type

Type-only change, separated so the behavioural frontend change in Task 5 has a
type to lean on.

**Files:**
- Modify: `frontend/src/api/types.ts:389-398`

- [ ] **Step 1: Add the field**

In `interface MetadataField`, after `suggested: boolean;`:

```typescript
  /** True when `options` is a set of suggestions from a vocabulary owned
   *  elsewhere (NCBI, an instrument vendor). Renders as a free-text combo
   *  rather than a <select>; see SchemaMetadataEditor. */
  open_vocabulary: boolean;
```

- [ ] **Step 2: Typecheck**

**`docker compose -p biopipe exec web npm run lint` checks the wrong tree from a
worktree.** `biopipe-web-1` bind-mounts the *main checkout's* `frontend/src`,
not this worktree's — confirmed via `docker inspect biopipe-web-1 --format
'{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}'`. Running the container command
here silently typechecks unmodified main and reports success regardless of
what this worktree's code says. Run `tsc` directly against the worktree
instead, using its own `node_modules`:

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors. `MetadataField` is only constructed from API responses,
never as an object literal in the frontend, so adding a required property
breaks nothing. If `tsc` reports an object literal missing `open_vocabulary`,
that is a real construction site — add the property there rather than making
the type optional.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/api/types.ts
git commit -m "feat(frontend): type open_vocabulary on MetadataField (#66)"
```

---

### Task 5: Render open fields as a combo

**Files:**
- Modify: `frontend/src/components/SchemaMetadataEditor.tsx:280-295`

This task is verified by typecheck plus the manual browser check in Task 6.

A precision worth having: the repo *does* run vitest (`npm test`, 9 test files
under `src/lib/` and elsewhere), so `CLAUDE.md`'s "no headless
component-testing setup" is about **components** specifically, and it is
accurate — there is no jsdom and no testing-library, every existing test is a
`.test.ts` over pure functions, and none renders a component. So this change
cannot get a unit test, but do not conclude from that that the frontend has no
tests at all; if you extract pure logic while working here, it can be tested.

- [ ] **Step 1: Replace the enum branch**

The current code at line 280 is:

```tsx
      {field.type === "enum" ? (
        <select
          value={str}
          onChange={(e) => onChange(e.target.value)}
          style={{ width: "100%", padding: "5px 6px", fontSize: 13 }}
        >
          <option value="">—</option>
          {field.options.map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
          {/* A stored value outside the suggested list must remain selectable,
              or saving the form would silently discard it. */}
          {str && !field.options.includes(str) && <option value={str}>{str}</option>}
        </select>
      ) : field.type === "boolean" ? (
```

Replace it with a three-way branch. The open case comes first:

```tsx
      {field.type === "enum" && field.open_vocabulary ? (
        /* A combo, not a <select>: these options come from a vocabulary we do
           not own, so the list is suggestions and the real answer is often not
           on it. A <select> here actively prevented recording the instrument
           models SRA writes ("NextSeq 550"), which is #66. Same idiom as
           ModelCombo.tsx, which argues the identical case for model ids. */
        <>
          <input
            list={`meta-${field.key}-options`}
            value={str}
            onChange={(e) => onChange(e.target.value)}
            spellCheck={false}
            autoComplete="off"
            style={{ width: "100%", padding: "5px 6px", fontSize: 13 }}
          />
          <datalist id={`meta-${field.key}-options`}>
            {field.options.map((o) => (
              <option key={o} value={o} />
            ))}
          </datalist>
        </>
      ) : field.type === "enum" ? (
        <select
          value={str}
          onChange={(e) => onChange(e.target.value)}
          style={{ width: "100%", padding: "5px 6px", fontSize: 13 }}
        >
          <option value="">—</option>
          {field.options.map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
          {/* A stored value outside the suggested list must remain selectable,
              or saving the form would silently discard it. */}
          {str && !field.options.includes(str) && <option value={str}>{str}</option>}
        </select>
      ) : field.type === "boolean" ? (
```

Two details that matter:

- The `datalist` id is per-field (`meta-${field.key}-options`). Several combos
  render on one form; a shared id silently binds every one of them to the first
  field's suggestions.
- The closed branch keeps its fallback `<option>`. A closed field can still hold
  an off-list legacy value, and that value must stay selectable.

- [ ] **Step 2: Typecheck**

Same caveat as Task 4: `docker compose -p biopipe exec web npm run lint` mounts
the *main checkout's* source, not this worktree's, and would report success
even if this file didn't compile. Run against the worktree directly:

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/SchemaMetadataEditor.tsx
git commit -m "feat(frontend): render open-vocabulary fields as a combo (#66)"
```

---

### Task 6: Verify against the real database and the running UI

The unit tests feed hand-built `FieldDef`s that already look how the rules
expect — the exact shape that let the Actions-tab suggestion rules pass green
while being wrong (`CLAUDE.md`, "Check a rule against the real database"). This
task is what actually proves the fix.

**Files:** none modified.

- [ ] **Step 1: Bring up the worktree stack**

```bash
./ops/worktree-up.sh
```

UI on 5273, API on 8100, its own database seeded from the main stack. Do **not**
use plain `docker compose` from this worktree.

- [ ] **Step 2: Count the warnings that remain**

The before-number is **53 warnings across 51 objects**, measured on 2026-08-06.
Run against the worktree stack's api container:

`worktree-up.sh` names its project `biopipe-wt-<branch-slug>`, so the container
depends on your branch. Derive it rather than typing it:

```bash
API=$(docker ps --format '{{.Names}}' | grep '^biopipe-wt-.*-api-1$')
echo "using $API"
```

```bash
docker exec "$API" python -c "
import asyncio
from app.db.client import connect_to_mongo, get_db
from app.metadata.schemas import coerce_and_validate
KEYS = ('platform', 'organism', 'reference_build', 'assay')
async def main():
    await connect_to_mongo()
    db = get_db()
    n = total = 0
    cur = db.objects.find(
        {'\$or': [{f'metadata.{k}': {'\$exists': True}} for k in KEYS]},
        {'metadata': 1, 'role': 1, 'format': 1},
    )
    async for d in cur:
        n += 1
        kind = (d.get('format') or {}).get('kind')
        r = coerce_and_validate(d.get('metadata') or {}, kind, d.get('role'))
        for w in r.warnings:
            if w['key'] in KEYS:
                total += 1
                print('STILL WARNING:', w['key'], w['message'][:70])
    print(f'objects scanned: {n}  warnings: {total}  (was 53)')
asyncio.run(main())
"
```

Expected: `warnings: 0` and no `STILL WARNING` lines.

Pass `format["kind"]`, not `format` — `format` is a nested document on the
object, and passing it whole raises `TypeError: unhashable type: 'dict'`.

Any `STILL WARNING` line names a field that needs `open_vocabulary=True` and was
missed in Task 2. Add it there, and add its key to `OPEN_ENUM_FIELDS` in
`tests/metadata/test_schemas_open_vocabulary.py`.

- [ ] **Step 3: Check the UI at localhost:5273**

Open a FASTQ whose `metadata.platform` is `MinION` (e.g. `DRR1078403.fastq`)
and confirm:

- Sequencing platform renders as a **text box with a dropdown arrow**, not a
  plain select, and shows `MinION`.
- Clicking it still suggests the family names (`Illumina NovaSeq`, …), and
  `Other` is **gone** from that list.
- Typing `PromethION` and saving stores it, with **no** warning shown.
- `Read type` still renders as a plain `<select>` with only single-end and
  paired-end.

- [ ] **Step 4: Confirm the API response carries the flag**

The route is `GET /api/v1/metadata/schemas/{kind}` — a path segment, not a
query parameter (`app/api/v1/search.py:116`).

```bash
curl -s localhost:8100/api/v1/metadata/schemas/fastq | python3 -c "import json,sys; f={x['key']:x for g in json.load(sys.stdin)['groups'] for x in g['fields']}; print('platform:', f['platform']['open_vocabulary']); print('read_type:', f['read_type']['open_vocabulary']); print('Other in platform options:', 'Other' in f['platform']['options'])"
```

Expected:

```
platform: True
read_type: False
Other in platform options: False
```

- [ ] **Step 5: Run the full suite one last time**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: **1 failed** (the pre-existing provenance-verb test), ~3503 passed.
Read the count — do not trust an exit code.

- [ ] **Step 6: Tear down**

```bash
./ops/worktree-up.sh --down
```

---

### Task 7: Close out the paperwork

**Files:**
- Modify: `docs/TODO.md` / `docs/TODO-done.md` (only if an entry covers this)

- [ ] **Step 1: Check whether a TODO entry covers this work**

```bash
grep -n "platform\|vocabulary\|dropdown" docs/TODO.md
```

If an entry covers it: append ` — FIXED` to its heading, note what shipped and
where, record the 53→0 measurement, say what this implementation did
differently from the spec, and move the whole entry to `docs/TODO-done.md`. If
no entry covers it, do nothing here — this came from issue #66, not the backlog.

- [ ] **Step 2: Merge to main and push**

Per `CLAUDE.md`, once the suite is green and `main` is clean, merge and push
without asking. Re-run the suite after merging if `main` has moved.

```bash
git checkout main && git merge --no-ff claude/issue-66-spec-impl-f7ffce && git push origin main
```

- [ ] **Step 3: Update the issue**

```bash
gh issue comment 66 --body "Implemented via open-vocabulary metadata fields. Measured 53 spurious warnings across 51 objects before, 0 after. Declined the issue's options 2 and 3: the dropdown vocabulary had no users, so there was no fragmentation to migrate. See docs/superpowers/specs/2026-08-06-open-vocabulary-metadata-fields-design.md"
gh issue close 66
```

Also drop the `status:implementation plan` label if it is still set.

---

## Notes for the implementer

**What must not break.** `sam_platform()` and its five read paths
(`_qc_platform`, `default_read_group`, `default_library`, `is_short_read`,
`is_long_read`) are untouched by design. They already consume instrument models
correctly — the substring funnel was *built* for them. If you find yourself
editing `pipeline_service.py`, stop: you have left this plan's scope.

**The trade this makes, knowingly.** A combo accepts typos. `Illumina NovaSq`
will save silently where the `<select>` made it unrepresentable. That is
accepted: the `<select>` was blocking the *correct* values while warning on all
of them, and this module's own docstring says its fields are suggestions, not
restrictions.

**`"Other"` may already be stored.** Removing it from the options lists does not
delete any stored value. A stored `"Other"` on an open field now coerces without
a warning (open fields never warn); on a closed field there is no `"Other"` to
begin with. Nothing migrates.
