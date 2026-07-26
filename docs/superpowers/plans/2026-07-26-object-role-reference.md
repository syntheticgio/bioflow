# Object Role / Reference Conversion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user declare a reads file to be a reference genome (and undo it) from the detail panel, so it re-sections in the left panel and shows assembly-appropriate metadata and facts.

**Architecture:** A new nullable `role` field on `DataObject` acts as an *override* on top of format-derived categorization. `role = None` means "derive from format," which is today's behavior; `role = "reference"` overrides it. Role short-circuits format when selecting the metadata schema, and the frontend renders a reference-specific detail panel. Conversion is a plain `PATCH /objects/{id}` — no new endpoint, no re-ingest.

**Tech Stack:** FastAPI + Beanie (MongoDB ODM) + Pydantic v2 on the backend; React + TanStack Query + react-router on the frontend. Tests are pytest (`asyncio_mode = "auto"`).

**Design spec:** `docs/superpowers/specs/2026-07-26-object-role-reference-design.md`

---

## Background for the engineer

Some things about this codebase that will not be obvious:

**Metadata is deliberately permissive.** `app/metadata/schemas.py` defines
suggested fields per format. Unknown keys are stored verbatim and invalid values
produce warnings rather than rejections. Do not add validation that refuses
input — that is a deliberate design choice, documented at the top of that file.
It is also what makes role conversion safe: fields belonging to the *previous*
role survive as unknown keys rather than being deleted.

**`update_object` uses a `.get(k) is not None` idiom.** In
`app/services/object_service.py`, each field is applied only when
`updates.get(key) is not None`. That idiom cannot express "clear this field,"
which is exactly what converting *back* to reads requires. Task 3 deliberately
breaks the pattern for `role` by testing `"role" in updates`. This is the single
subtlest point in the plan — get it wrong and conversion is one-way.

**`FormatKind.FASTA` currently maps to `REFERENCE_FIELDS`.** That mapping is the
bug this feature replaces: it assumes any FASTA is a reference. Task 4 changes
it to `()`, so a plain FASTA gets common fields only and reference fields come
from *role* instead.

**Run tests inside Docker.** `make test` runs `docker compose exec -T api pytest -v`.
The stack must be up (`make up`). To run one file:
`docker compose exec -T api pytest tests/storage/test_metadata_schemas.py -v`

---

## File Structure

**Backend — modify:**
- `app/models/object.py` — add `ObjectRole` enum, `role` field, `by_role` index
- `app/models/__init__.py` — export `ObjectRole`
- `app/api/v1/schemas.py` — `role` on `ObjectUpdate` and `ObjectOut`
- `app/services/object_service.py` — apply role in `update_object`, pass role to metadata coercion
- `app/metadata/schemas.py` — role short-circuits format; expand `REFERENCE_FIELDS`; `FASTA → ()`
- `app/api/v1/search.py` — `role` query param on `GET /metadata/schemas/{kind}`

**Backend — create:**
- `tests/storage/test_object_role.py` — role model + service behavior

**Backend — modify tests:**
- `tests/storage/test_metadata_schemas.py` — role-aware schema resolution

**Frontend — modify:**
- `src/api/types.ts` — `ObjectRole` type, `role` on `DataObject`
- `src/api/client.ts` — `metadataSchema` takes optional role
- `src/components/ProjectExplorer.tsx` — categorize on role; reference icon
- `src/components/SchemaMetadataEditor.tsx` — accept and forward `role`
- `src/lib/format.ts` — `assembly_accession` accession link
- `src/components/DetailPanel.tsx` — role branch, Assembly section, convert control

**Frontend — create:**
- `src/components/AssemblyFacts.tsx` — curated reference facts display
- `src/components/RoleConverter.tsx` — the convert/revert control

`DetailPanel.tsx` is already 525 lines. The two new components keep this change
from pushing it toward 700, and both are independently testable units with a
single responsibility.

---

## Task 1: Add the ObjectRole enum and role field

**Files:**
- Modify: `backend/app/models/object.py`
- Modify: `backend/app/models/__init__.py`
- Test: `backend/tests/storage/test_object_role.py` (create)

- [ ] **Step 1: Write the failing test**

Create `backend/tests/storage/test_object_role.py`:

```python
"""Object role: the override that distinguishes a reference from reads."""

from app.models import FormatKind, ObjectRole
from app.models.object import DataObject
from beanie import PydanticObjectId


def _obj(**kw) -> DataObject:
    """A DataObject built without touching the database."""
    defaults = dict(project_id=PydanticObjectId(), name="sample.fastq.gz")
    return DataObject(**{**defaults, **kw})


class TestObjectRole:
    def test_role_defaults_to_none(self):
        """None means 'derive the category from the format', today's behavior."""
        assert _obj().role is None

    def test_role_accepts_reference(self):
        assert _obj(role=ObjectRole.REFERENCE).role is ObjectRole.REFERENCE

    def test_role_is_a_string_enum(self):
        """StrEnum so it serializes to plain JSON without a custom encoder."""
        assert ObjectRole.REFERENCE == "reference"
        assert _obj(role="reference").role is ObjectRole.REFERENCE

    def test_role_round_trips_through_serialization(self):
        dumped = _obj(role=ObjectRole.REFERENCE).model_dump(mode="json")
        assert dumped["role"] == "reference"
        assert _obj(**{"role": dumped["role"]}).role is ObjectRole.REFERENCE

    def test_format_kind_is_independent_of_role(self):
        """A reference can be FASTQ; the whole point is that format does not decide."""
        o = _obj(role=ObjectRole.REFERENCE)
        o.format.kind = FormatKind.FASTQ
        assert o.role is ObjectRole.REFERENCE
        assert o.format.kind is FormatKind.FASTQ
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `docker compose exec -T api pytest tests/storage/test_object_role.py -v`

Expected: FAIL — `ImportError: cannot import name 'ObjectRole' from 'app.models'`

- [ ] **Step 3: Add the enum and field**

In `backend/app/models/object.py`, add after the `Compression` enum (around line 50):

```python
class ObjectRole(StrEnum):
    """How a file is *used*, when that cannot be read from its bytes.

    A reference genome and a set of reads can both be FASTA or FASTQ; which one
    a file is depends on the user's intent. Role records that intent, and is
    left unset for the common case where the detected format already implies
    the answer (a BAM is an alignment, a VCF is variants).

    Left as an enum rather than a boolean because formats such as WIG have more
    than two plausible roles; those extend this enum without a schema change.
    """

    REFERENCE = "reference"
```

Then on `DataObject`, add after the `tags` field (around line 102):

```python
    # None means "derive the category from the format". Only exceptions carry
    # a value, so re-ingest can never fight a user's explicit choice.
    role: ObjectRole | None = None
```

And add to `Settings.indexes`:

```python
            IndexModel([("project_id", ASCENDING), ("role", ASCENDING)], name="by_role"),
```

- [ ] **Step 4: Export the enum**

In `backend/app/models/__init__.py`, add `ObjectRole` to the existing
`app.models.object` import block, keeping it alphabetical:

```python
from app.models.object import (
    Compression,
    DataObject,
    FormatConfidence,
    FormatInfo,
    FormatKind,
    ObjectError,
    ObjectRole,
    ObjectStatus,
    SourceInfo,
    SourceMode,
)
```

Then add `"ObjectRole",` to `__all__`, which is alphabetically sorted — place it
between `"ObjectError"` and `"ObjectStatus"`. `ALL_MODELS` does not change:
`ObjectRole` is an enum, not a Beanie document.

- [ ] **Step 5: Run the test to verify it passes**

Run: `docker compose exec -T api pytest tests/storage/test_object_role.py -v`

Expected: PASS, 5 passed

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/object.py backend/app/models/__init__.py backend/tests/storage/test_object_role.py
git commit -m "feat: add ObjectRole field to DataObject"
```

---

## Task 2: Expose role through the API schemas

**Files:**
- Modify: `backend/app/api/v1/schemas.py:68-71` (ObjectUpdate), `:98-131` (ObjectOut)
- Test: `backend/tests/storage/test_object_role.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/storage/test_object_role.py`:

```python
from app.api.v1.schemas import ObjectOut, ObjectUpdate


class TestRoleSerialization:
    def test_object_out_exposes_role(self):
        out = ObjectOut.of(_obj(role=ObjectRole.REFERENCE))
        assert out.role == "reference"

    def test_object_out_role_is_none_when_unset(self):
        assert ObjectOut.of(_obj()).role is None

    def test_update_distinguishes_omitted_from_explicit_null(self):
        """The whole reversibility story rests on this distinction.

        An omitted role must not appear in the dump (so a rename leaves role
        alone), while an explicit null must appear (so 'convert back' can clear
        it).
        """
        omitted = ObjectUpdate(name="x").model_dump(exclude_unset=True)
        assert "role" not in omitted

        explicit = ObjectUpdate(role=None).model_dump(exclude_unset=True)
        assert "role" in explicit
        assert explicit["role"] is None

    def test_update_accepts_a_role_value(self):
        dumped = ObjectUpdate(role=ObjectRole.REFERENCE).model_dump(exclude_unset=True)
        assert dumped["role"] is ObjectRole.REFERENCE
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `docker compose exec -T api pytest tests/storage/test_object_role.py::TestRoleSerialization -v`

Expected: FAIL — `AttributeError`/`ValidationError` on the unknown `role` field

- [ ] **Step 3: Add role to both schemas**

In `backend/app/api/v1/schemas.py`, import `ObjectRole` alongside the existing
model imports, then change `ObjectUpdate`:

```python
class ObjectUpdate(BaseModel):
    name: str | None = None
    metadata: dict | None = None
    tags: list[str] | None = None
    # An explicit null clears the role ("convert back to reads"); omitting the
    # key leaves it untouched. exclude_unset=True in the route preserves the
    # difference.
    role: ObjectRole | None = None
```

On `ObjectOut`, add the field `role: str | None` and, in `ObjectOut.of`, add:

```python
            role=o.role.value if o.role else None,
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `docker compose exec -T api pytest tests/storage/test_object_role.py -v`

Expected: PASS, 9 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/v1/schemas.py backend/tests/storage/test_object_role.py
git commit -m "feat: expose object role through the API schemas"
```

---

## Task 3: Apply role in update_object

This is the subtlest task in the plan. `update_object` applies every other field
with `updates.get(key) is not None`, which cannot express "clear it." Role must
use `"role" in updates` instead.

**Files:**
- Modify: `backend/app/services/object_service.py:317-346`
- Test: `backend/tests/storage/test_object_role.py`

- [ ] **Step 1: Write the failing test**

The service hits the database, so test the *decision logic* directly. Add a
pure helper to keep it testable, then assert on it.

Append to `backend/tests/storage/test_object_role.py`:

```python
from app.services.object_service import apply_role_update


class TestApplyRoleUpdate:
    """Role is the one field where explicit-null differs from omitted."""

    def test_omitted_role_leaves_the_existing_value(self):
        obj = _obj(role=ObjectRole.REFERENCE)
        apply_role_update(obj, {"name": "renamed.fa"})
        assert obj.role is ObjectRole.REFERENCE

    def test_explicit_null_clears_the_role(self):
        obj = _obj(role=ObjectRole.REFERENCE)
        apply_role_update(obj, {"role": None})
        assert obj.role is None

    def test_setting_a_role(self):
        obj = _obj()
        apply_role_update(obj, {"role": ObjectRole.REFERENCE})
        assert obj.role is ObjectRole.REFERENCE

    def test_string_role_is_coerced_to_the_enum(self):
        """The route hands over whatever survived Pydantic; accept both."""
        obj = _obj()
        apply_role_update(obj, {"role": "reference"})
        assert obj.role is ObjectRole.REFERENCE

    def test_round_trip_conversion_returns_to_the_starting_state(self):
        obj = _obj()
        apply_role_update(obj, {"role": ObjectRole.REFERENCE})
        apply_role_update(obj, {"role": None})
        assert obj.role is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `docker compose exec -T api pytest tests/storage/test_object_role.py::TestApplyRoleUpdate -v`

Expected: FAIL — `ImportError: cannot import name 'apply_role_update'`

- [ ] **Step 3: Implement the helper and call it**

In `backend/app/services/object_service.py`, add above `update_object`:

```python
def apply_role_update(obj: DataObject, updates: dict) -> None:
    """Apply a role change, distinguishing an explicit null from an omission.

    Every other field in update_object uses `.get(k) is not None`, which treats
    null and absent alike. Role cannot: clearing it is how a reference is
    converted back to reads, so the *presence of the key* is what matters.
    """
    if "role" not in updates:
        return
    raw = updates["role"]
    obj.role = ObjectRole(raw) if raw is not None else None
```

Import `ObjectRole` from `app.models` at the top of the file, then call the
helper inside `update_object`, immediately before `obj.touch()`:

```python
    apply_role_update(obj, updates)

    obj.touch()
    await obj.save()
    return obj
```

- [ ] **Step 4: Pass role to metadata coercion**

Still in `update_object`, the metadata branch currently coerces against format
alone. Values must be coerced against whatever role the object will have *after*
this update, so a single PATCH that sets role and metadata together behaves
consistently. Move `apply_role_update` to run *before* the metadata branch, and
change the coercion call:

```python
        validated = schemas.coerce_and_validate(
            updates["metadata"], obj.format.kind, role=obj.role
        )
```

The final ordering inside `update_object` is: name, tags, **role**, metadata,
`touch()`, `save()`. (`schemas.coerce_and_validate` grows its `role` parameter in
Task 4; this line will not run correctly until that task lands, which is fine —
the tests for it live in Task 4.)

- [ ] **Step 5: Run the test to verify it passes**

Run: `docker compose exec -T api pytest tests/storage/test_object_role.py::TestApplyRoleUpdate -v`

Expected: PASS, 5 passed

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/object_service.py backend/tests/storage/test_object_role.py
git commit -m "feat: apply role updates, distinguishing null from omitted"
```

---

## Task 4: Make the metadata schema role-aware

**Files:**
- Modify: `backend/app/metadata/schemas.py:249-255` (REFERENCE_FIELDS), `:267-278` (FORMAT_FIELDS), `:281-309`, `:321-346`, `:399-418`
- Test: `backend/tests/storage/test_metadata_schemas.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/storage/test_metadata_schemas.py`:

```python
from app.models import ObjectRole


class TestRoleAwareFields:
    def test_reference_role_replaces_format_fields(self):
        """A reference FASTQ is a genome build, not a sequencing run: library
        and flowcell questions stop being meaningful."""
        keys = {f.key for f in schemas.fields_for(FormatKind.FASTQ, role=ObjectRole.REFERENCE)}
        assert "reference_build" in keys
        assert "assembly_accession" in keys
        assert "library_prep" not in keys
        assert "flowcell" not in keys

    def test_reference_role_keeps_common_fields(self):
        keys = {f.key for f in schemas.fields_for(FormatKind.FASTQ, role=ObjectRole.REFERENCE)}
        assert {"sample_id", "organism", "notes"} <= keys

    def test_reference_role_applies_regardless_of_format(self):
        fastq = {f.key for f in schemas.fields_for(FormatKind.FASTQ, role=ObjectRole.REFERENCE)}
        fasta = {f.key for f in schemas.fields_for(FormatKind.FASTA, role=ObjectRole.REFERENCE)}
        assert fastq == fasta

    def test_plain_fasta_is_no_longer_assumed_to_be_a_reference(self):
        """Reference fields now come from role, not from the format."""
        keys = {f.key for f in schemas.fields_for(FormatKind.FASTA)}
        assert keys == {f.key for f in schemas.COMMON_FIELDS}
        assert "assembly_accession" not in keys

    def test_fastq_without_a_role_is_unaffected(self):
        keys = {f.key for f in schemas.fields_for(FormatKind.FASTQ)}
        assert "library_prep" in keys

    def test_schema_for_api_accepts_a_role(self):
        out = schemas.schema_for_api(FormatKind.FASTQ, role=ObjectRole.REFERENCE)
        groups = {g["group"] for g in out["groups"]}
        assert "Reference" in groups
        assert "Library" not in groups


class TestMetadataSurvivesConversion:
    """Reversibility depends on old-role values not being destroyed."""

    def test_previous_role_values_are_kept_as_unknown_keys(self):
        result = schemas.coerce_and_validate(
            {"flowcell": "HXXXDSX3", "lane": 4, "reference_build": "GRCh38"},
            FormatKind.FASTQ,
            role=ObjectRole.REFERENCE,
        )
        assert result.values["flowcell"] == "HXXXDSX3"
        assert result.values["reference_build"] == "GRCh38"

    def test_round_trip_conversion_preserves_values(self):
        original = {"flowcell": "HXXXDSX3", "library_prep": "TruSeq"}
        as_reference = schemas.coerce_and_validate(
            original, FormatKind.FASTQ, role=ObjectRole.REFERENCE
        ).values
        back_to_reads = schemas.coerce_and_validate(
            as_reference, FormatKind.FASTQ, role=None
        ).values
        assert back_to_reads["flowcell"] == "HXXXDSX3"
        assert back_to_reads["library_prep"] == "TruSeq"


class TestReferenceFieldDefinitions:
    def test_new_reference_fields_exist_with_expected_types(self):
        fields = schemas.field_map(None, role=ObjectRole.REFERENCE)
        assert fields["assembly_accession"].type is FieldType.TEXT
        assert fields["is_primary_assembly"].type is FieldType.BOOLEAN
        assert fields["has_decoy"].type is FieldType.BOOLEAN
        assert fields["index_types"].type is FieldType.TEXT
        assert fields["masked"].type is FieldType.BOOLEAN

    def test_reference_build_stays_free_text(self):
        """Builds are open-ended (custom assemblies, patches), so no enum."""
        spec = schemas.field_map(None, role=ObjectRole.REFERENCE)["reference_build"]
        assert spec.type is FieldType.TEXT
        assert spec.options == ()

    def test_reference_fields_are_grouped_together(self):
        fields = schemas.field_map(None, role=ObjectRole.REFERENCE)
        for key in ("reference_build", "source", "assembly_accession", "has_decoy"):
            assert fields[key].group == "Reference"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `docker compose exec -T api pytest tests/storage/test_metadata_schemas.py -v`

Expected: FAIL — `TypeError: fields_for() got an unexpected keyword argument 'role'`

- [ ] **Step 3: Expand REFERENCE_FIELDS**

In `backend/app/metadata/schemas.py`, replace `REFERENCE_FIELDS` (lines 249-255):

```python
REFERENCE_FIELDS: tuple[FieldDef, ...] = (
    # Free text rather than an enum: reference builds are open-ended, including
    # custom assemblies and patch releases that no fixed list would cover.
    FieldDef("reference_build", "Build", group="Reference", suggested=True),
    FieldDef("source", "Source", help="e.g. Ensembl release 110, UCSC, NCBI RefSeq.",
             group="Reference", suggested=True),
    FieldDef("assembly_accession", "Assembly accession",
             help="e.g. GCA_000001405.29. The unambiguous identifier for this assembly.",
             group="Reference", suggested=True),
    FieldDef("is_primary_assembly", "Primary assembly only", type=FieldType.BOOLEAN,
             help="Alt and patch contigs excluded. Mixing this up is a common "
                  "source of surprising alignment results.",
             group="Reference", suggested=True),
    FieldDef("has_decoy", "Includes decoy contigs", type=FieldType.BOOLEAN,
             help="e.g. hs38d1. Affects aligner choice and mapping rates.",
             group="Reference", suggested=True),
    FieldDef("index_types", "Aligner indexes",
             help="Which indexes have been built, e.g. BWA, bowtie2, STAR.",
             group="Reference", suggested=True),
    FieldDef("masked", "Masked", type=FieldType.BOOLEAN,
             help="Repeat-masked sequence.", group="Reference"),
)
```

- [ ] **Step 4: Unmap FASTA from REFERENCE_FIELDS**

In the `FORMAT_FIELDS` dict, change the FASTA entry:

```python
    # A FASTA is no longer assumed to be a reference -- that now comes from the
    # object's role, so a FASTA of reads is not asked reference questions.
    FormatKind.FASTA: (),
```

- [ ] **Step 5: Thread role through the resolution functions**

Replace `fields_for`, `field_map`, and `all_known_fields` (lines 281-309):

```python
def fields_for(
    kind: FormatKind | str | None, role: "ObjectRole | str | None" = None
) -> list[FieldDef]:
    """Common fields plus anything specific to this file.

    Role wins outright over format when set: once a file is declared a
    reference, its library and sequencing fields are noise rather than context.
    Format-specific definitions win on key collisions with common ones.
    """
    if isinstance(kind, str):
        try:
            kind = FormatKind(kind)
        except ValueError:
            kind = None

    if isinstance(role, str):
        try:
            role = ObjectRole(role)
        except ValueError:
            role = None

    if role is ObjectRole.REFERENCE:
        specific: tuple[FieldDef, ...] = REFERENCE_FIELDS
    else:
        specific = FORMAT_FIELDS.get(kind, ()) if kind else ()

    by_key: dict[str, FieldDef] = {f.key: f for f in COMMON_FIELDS}
    by_key.update({f.key: f for f in specific})
    return list(by_key.values())


def field_map(
    kind: FormatKind | str | None = None, role: "ObjectRole | str | None" = None
) -> dict[str, FieldDef]:
    return {f.key: f for f in fields_for(kind, role)}


def all_known_fields() -> dict[str, FieldDef]:
    """Every field across every format and role, for validating unscoped edits."""
    out: dict[str, FieldDef] = {f.key: f for f in COMMON_FIELDS}
    for group in FORMAT_FIELDS.values():
        for f in group:
            out.setdefault(f.key, f)
    for f in REFERENCE_FIELDS:
        out.setdefault(f.key, f)
    return out
```

`REFERENCE_FIELDS` must be added to `all_known_fields` explicitly now that it is
no longer reachable via `FORMAT_FIELDS`. Without that, reference fields would be
treated as unknown keys during unscoped edits and skip type coercion.

Add the import at the top of the file, beside the existing `FormatKind` import:

```python
from app.models import FormatKind, ObjectRole
```

- [ ] **Step 6: Thread role through coercion and the API shape**

Change the signature of `coerce_and_validate` (line 321) and its first body line:

```python
def coerce_and_validate(
    metadata: dict,
    kind: FormatKind | str | None = None,
    role: "ObjectRole | str | None" = None,
) -> ValidationResult:
```

```python
    known = field_map(kind, role) or {}
```

Leave the rest of the function alone — the existing `all_known_fields()` fallback
is what preserves previous-role values, and it already does the right thing.

Then `schema_for_api` (line 399):

```python
def schema_for_api(
    kind: FormatKind | str | None = None, role: "ObjectRole | str | None" = None
) -> dict:
    """Grouped field definitions, ordered for rendering."""
    fields = fields_for(kind, role)
```

and in its return dict, add the role so the client can confirm what it received:

```python
    return {
        "kind": kind.value if isinstance(kind, FormatKind) else kind,
        "role": role.value if isinstance(role, ObjectRole) else role,
        "groups": ordered,
    }
```

- [ ] **Step 7: Run the full metadata suite**

Run: `docker compose exec -T api pytest tests/storage/test_metadata_schemas.py -v`

Expected: PASS. Existing tests must still pass — note
`test_unknown_kind_falls_back_to_common_fields` and the FASTA cases in
`test_common_fields_apply_to_every_format` already expect common-only behavior,
so the `FASTA → ()` change does not break them.

- [ ] **Step 8: Run the whole backend suite**

Run: `docker compose exec -T api pytest -v`

Expected: PASS. If anything outside these files fails, it is calling
`coerce_and_validate` or `fields_for` positionally — all new parameters are
keyword-with-default, so this should not happen; investigate rather than
loosening the signature.

- [ ] **Step 9: Commit**

```bash
git add backend/app/metadata/schemas.py backend/tests/storage/test_metadata_schemas.py
git commit -m "feat: make metadata schema resolution role-aware"
```

---

## Task 5: Accept role on the schema endpoint

**Files:**
- Modify: `backend/app/api/v1/search.py:97-111`

- [ ] **Step 1: Add the query parameter**

Replace `get_schema`:

```python
@router.get("/metadata/schemas/{kind}")
async def get_schema(kind: str, role: str | None = None) -> dict:
    """Suggested fields for one format, optionally narrowed by role.

    These are suggestions, not restrictions: arbitrary keys remain allowed and
    are stored as-is.
    """
    try:
        format_kind = FormatKind(kind)
    except ValueError as e:
        raise ValidationError(
            f"Unknown format kind: {kind!r}",
            details={"known": [k.value for k in FormatKind]},
        ) from e

    object_role: ObjectRole | None = None
    if role:
        try:
            object_role = ObjectRole(role)
        except ValueError as e:
            raise ValidationError(
                f"Unknown role: {role!r}",
                details={"known": [r.value for r in ObjectRole]},
            ) from e

    return meta_schemas.schema_for_api(format_kind, role=object_role)
```

Add `ObjectRole` to the existing `from app.models import ...` line in this file.

- [ ] **Step 2: Verify by hand**

Run:

```bash
curl -s "http://localhost:8000/api/v1/metadata/schemas/fastq?role=reference" | python3 -m json.tool | head -30
```

Expected: a `Reference` group containing `assembly_accession`, and no `Library`
group. Then confirm the unroled call is unchanged:

```bash
curl -s "http://localhost:8000/api/v1/metadata/schemas/fastq" | python3 -m json.tool | grep -c library_prep
```

Expected: `1`

- [ ] **Step 3: Confirm a bad role is rejected cleanly**

```bash
curl -s -o /dev/null -w "%{http_code}\n" "http://localhost:8000/api/v1/metadata/schemas/fastq?role=nonsense"
```

Expected: `400`

- [ ] **Step 4: Commit**

```bash
git add backend/app/api/v1/search.py
git commit -m "feat: accept a role query param on the schema endpoint"
```

---

## Task 6: Frontend types and API client

**Files:**
- Modify: `frontend/src/api/types.ts:42-57`
- Modify: `frontend/src/api/client.ts:175-176`

- [ ] **Step 1: Add the role type**

In `frontend/src/api/types.ts`, above `interface DataObject`:

```ts
/** How a file is used, when its format cannot say. Null = derive from format. */
export type ObjectRole = "reference";
```

and add to `DataObject`, after `tags`:

```ts
  role: ObjectRole | null;
```

- [ ] **Step 2: Add role to the MetadataSchema type**

Still in `types.ts`, find `interface MetadataSchema` and add:

```ts
  role: ObjectRole | null;
```

- [ ] **Step 3: Pass role from the client**

In `frontend/src/api/client.ts`, replace `metadataSchema`:

```ts
  metadataSchema: (kind: string, role?: ObjectRole | null) =>
    request<MetadataSchema>(
      `/metadata/schemas/${encodeURIComponent(kind)}` +
        (role ? `?role=${encodeURIComponent(role)}` : ""),
    ),
```

Add `ObjectRole` to the type import at the top of `client.ts`.

- [ ] **Step 4: Verify it compiles**

Run: `npm --prefix frontend run lint`

Expected: PASS with no errors. If the `ui` service name differs, check
`docker-compose.yml`; alternatively run `npx tsc --noEmit` from `frontend/`.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/api/client.ts
git commit -m "feat: add object role to the frontend API types"
```

---

## Task 7: Categorize on role in the left panel

**Files:**
- Modify: `frontend/src/components/ProjectExplorer.tsx:148-160`, `:345-347`

- [ ] **Step 1: Replace the metadata check**

Replace `categorizeFile` (lines 148-160):

```ts
function categorizeFile(obj: DataObject): FileCategory {
  // Role is an override: when set it decides outright, because the format
  // cannot tell a reference genome from a pile of reads.
  if (obj.role === "reference") return "references";

  const kind = obj.format.kind.toLowerCase();
  if (kind === "fastq" || kind === "fasta") return "reads";
  if (["bam", "sam", "cram"].includes(kind)) return "alignments";
  if (["vcf", "bcf"].includes(kind)) return "variants";
  if (["bed", "gff", "gtf"].includes(kind)) return "annotations";
  if (kind === "hic") return "hic";
  return "other";
}
```

The `metadata.is_reference` check is gone — that flag was never set by anything,
and role replaces it.

- [ ] **Step 2: Give references a distinct icon**

Replace the row icon (line 345-347):

```tsx
                      <span className="row-icon">
                        {o.status !== "ready"
                          ? "⏳"
                          : o.role === "reference"
                            ? "📗"
                            : "📄"}
                      </span>
```

- [ ] **Step 3: Verify it compiles**

Run: `npm --prefix frontend run lint`

Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/ProjectExplorer.tsx
git commit -m "feat: categorize files by role in the project explorer"
```

---

## Task 8: Forward role to the metadata editor

**Files:**
- Modify: `frontend/src/components/SchemaMetadataEditor.tsx:7-29`

- [ ] **Step 1: Accept a role prop**

Change the `Props` interface:

```ts
interface Props {
  value: Record<string, unknown>;
  formatKind: string;
  role: ObjectRole | null;
  onSave: (next: Record<string, unknown>) => void;
  saving?: boolean;
}
```

Add `ObjectRole` to the type import from `../api/types`.

- [ ] **Step 2: Include role in the query**

```ts
export function SchemaMetadataEditor({ value, formatKind, role, onSave, saving }: Props) {
  const { data: schema } = useQuery({
    // Role is part of the key: a conversion changes which fields apply, and a
    // stale cache would serve the previous role's form.
    queryKey: ["metadata", "schema", formatKind, role],
    queryFn: () => api.metadataSchema(formatKind, role),
    staleTime: 5 * 60 * 1000, // schemas are static within a release
  });
```

- [ ] **Step 3: Pass it from the detail panel**

In `frontend/src/components/DetailPanel.tsx`, find the `SchemaMetadataEditor`
usage (around line 445) and add the prop:

```tsx
          <SchemaMetadataEditor
            value={obj.metadata}
            formatKind={obj.format.kind}
            role={obj.role}
            onSave={(m) => save.mutate(m)}
            saving={save.isPending}
          />
```

- [ ] **Step 4: Verify it compiles**

Run: `npm --prefix frontend run lint`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/SchemaMetadataEditor.tsx frontend/src/components/DetailPanel.tsx
git commit -m "feat: scope the metadata editor schema to the object role"
```

---

## Task 9: Link assembly accessions to NCBI

**Files:**
- Modify: `frontend/src/lib/format.ts:88-97` (inside `ACCESSION_LINKS`)

- [ ] **Step 1: Add the accession pattern**

In the `ACCESSION_LINKS` object, after the `biosample` entry:

```ts
  assembly_accession: {
    // GCA_ (GenBank) or GCF_ (RefSeq), nine digits, dot, version.
    pattern: /^GC[AF]_\d{9}\.\d+$/i,
    url: (v) => `https://www.ncbi.nlm.nih.gov/datasets/genome/${v}`,
    label: "Assembly",
  },
```

`accessionUrl` already uppercases the value and returns null on a pattern miss,
so a half-typed accession simply shows no link. No other change is needed —
`SchemaMetadataEditor` renders the link from the label automatically.

- [ ] **Step 2: Verify it compiles**

Run: `npm --prefix frontend run lint`

Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add frontend/src/lib/format.ts
git commit -m "feat: link assembly accessions to NCBI datasets"
```

---

## Task 10: The AssemblyFacts component

**Files:**
- Create: `frontend/src/components/AssemblyFacts.tsx`

- [ ] **Step 1: Create the component**

```tsx
import { useState } from "react";

interface Props {
  facts: Record<string, unknown>;
}

const MAX_VISIBLE_CONTIGS = 25;

/**
 * Curated facts for a reference assembly.
 *
 * The generic FactsTable dumps every parsed key, which buries the four numbers
 * that actually characterize a genome build. This shows those, and nothing
 * else.
 */
export function AssemblyFacts({ facts }: Props) {
  const [showAllContigs, setShowAllContigs] = useState(false);

  const exactCount = facts.sequence_count as number | undefined;
  const estimatedCount = facts.sequence_count_estimate as number | undefined;
  const isExact = facts.sequence_count_exact === true;
  const totalBases = facts.total_bases as number | undefined;
  const gc = facts.gc_content_percent as number | undefined;
  const sampledBases = facts.stats_sampled_bases as number | undefined;
  const names = Array.isArray(facts.sequence_names)
    ? (facts.sequence_names as string[])
    : [];
  const namesTruncated = facts.sequence_names_truncated === true;

  const count = isExact ? exactCount : estimatedCount;
  const hasAnything =
    count !== undefined || totalBases !== undefined || gc !== undefined;

  if (!hasAnything) {
    return (
      <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
        No assembly facts extracted yet.
      </div>
    );
  }

  return (
    <div>
      <dl className="kv">
        {count !== undefined && (
          <>
            <dt>Sequences</dt>
            <dd>
              {isExact ? count.toLocaleString() : `~${count.toLocaleString()}`}
              {!isExact && (
                <span style={{ color: "var(--text-faint)" }}> (estimated)</span>
              )}
            </dd>
          </>
        )}
        {totalBases !== undefined && (
          <>
            <dt>Total bases</dt>
            <dd>{formatBases(totalBases)}</dd>
          </>
        )}
        {gc !== undefined && (
          <>
            {/* Labelled as sampled because fasta_stats caps at 50M bases read
                from the start of the file -- on a large genome that is chr1,
                not a representative sample. See docs/TODO.md. */}
            <dt>GC content (sampled)</dt>
            <dd>
              {gc}%
              {sampledBases !== undefined && (
                <span style={{ color: "var(--text-faint)" }}>
                  {" "}
                  from {formatBases(sampledBases)} sampled
                </span>
              )}
            </dd>
          </>
        )}
      </dl>

      {names.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div
            style={{ fontSize: 11, color: "var(--text-faint)", marginBottom: 6 }}
          >
            Sequences
          </div>
          <div
            className="mono"
            style={{
              fontSize: 12,
              display: "flex",
              flexWrap: "wrap",
              gap: "4px 10px",
            }}
          >
            {(showAllContigs ? names : names.slice(0, MAX_VISIBLE_CONTIGS)).map(
              (n) => (
                <span key={n}>{n}</span>
              ),
            )}
          </div>
          {names.length > MAX_VISIBLE_CONTIGS && (
            <button
              type="button"
              className="btn"
              style={{ marginTop: 8 }}
              onClick={() => setShowAllContigs(!showAllContigs)}
            >
              {showAllContigs
                ? "Show fewer"
                : `Show all ${names.length}`}
            </button>
          )}
          {namesTruncated && (
            <div
              style={{ color: "var(--text-faint)", fontSize: 11, marginTop: 6 }}
            >
              List truncated during parsing; the assembly has more sequences
              than are recorded here.
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** Base counts read better in Gb/Mb than as raw digits. */
function formatBases(n: number): string {
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)} Gb`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} Mb`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)} kb`;
  return `${n} bp`;
}
```

Note this component deliberately does *not* import `formatBytes` from
`../lib/format`. Bases are not bytes: `formatBytes` is 1024-based (KiB/MiB),
while a genome is measured in decimal Gb/Mb. Hence the local `formatBases`.

- [ ] **Step 2: Verify it compiles**

Run: `npm --prefix frontend run lint`

Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/AssemblyFacts.tsx
git commit -m "feat: add curated assembly facts display for references"
```

---

## Task 11: The RoleConverter component

**Files:**
- Create: `frontend/src/components/RoleConverter.tsx`

- [ ] **Step 1: Create the component**

```tsx
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { notify } from "../stores/messageStore";
import type { DataObject } from "../api/types";

interface Props {
  obj: DataObject;
}

/** Formats where reference-vs-reads is genuinely ambiguous. */
const CONVERTIBLE_FORMATS = ["fasta", "fastq"];

/**
 * Converts a file between reads and reference.
 *
 * Both directions are the same PATCH with a different value, and the change is
 * cheap and reversible -- so there is no confirmation step, which would be
 * friction without benefit.
 */
export function RoleConverter({ obj }: Props) {
  const qc = useQueryClient();
  const isReference = obj.role === "reference";

  const convert = useMutation({
    mutationFn: (role: "reference" | null) => api.updateObject(obj.id, { role }),
    onSuccess: (_r, role) => {
      qc.invalidateQueries({ queryKey: ["object", obj.id] });
      // The left panel re-sections off this value.
      qc.invalidateQueries({ queryKey: ["objects", obj.project_id] });
      qc.invalidateQueries({ queryKey: ["search"] });
      notify.success(
        role === "reference"
          ? `${obj.name} is now a reference`
          : `${obj.name} is now reads`,
      );
    },
    onError: (e: Error) => notify.error(e.message),
  });

  // A BAM or VCF has an unambiguous role already; offering to convert it
  // invites confusion rather than solving a problem.
  if (!isReference && !CONVERTIBLE_FORMATS.includes(obj.format.kind.toLowerCase())) {
    return null;
  }

  return (
    <div className="section">
      <div className="section-title">Role</div>
      <button
        type="button"
        className="btn"
        onClick={() => convert.mutate(isReference ? null : "reference")}
        disabled={convert.isPending}
      >
        {convert.isPending
          ? "Converting…"
          : isReference
            ? "Convert back to reads"
            : "Convert to reference"}
      </button>
      <div style={{ color: "var(--text-faint)", fontSize: 11, marginTop: 6 }}>
        {isReference
          ? "Moves this back to the Reads section and restores the sequencing metadata fields. Nothing is lost either way."
          : "Marks this as a reference genome. It will move to the References section and show assembly metadata."}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Verify it compiles**

Run: `npm --prefix frontend run lint`

Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/RoleConverter.tsx
git commit -m "feat: add the reads/reference conversion control"
```

---

## Task 12: Wire the detail panel to role

**Files:**
- Modify: `frontend/src/components/DetailPanel.tsx:283-300` (header), `:337-406` (facts), `:441` (SraPanel), `:474` (before Delete)

- [ ] **Step 1: Import the new components**

Add to the imports at the top of `DetailPanel.tsx`:

```tsx
import { AssemblyFacts } from "./AssemblyFacts";
import { RoleConverter } from "./RoleConverter";
```

- [ ] **Step 2: Derive the flag and retitle the header**

Immediately after the `formatDisagreement` const (around line 281):

```tsx
  const isReference = obj.role === "reference";
```

Then change the panel title (line 286):

```tsx
        <span className="panel-title">{isReference ? "Reference" : "File"}</span>
```

- [ ] **Step 3: Branch the facts section**

Replace the body of the "Parsed facts" section — the part inside
`{Object.keys(obj.facts).length > 0 ? ( ... ) : ( ... )}`. Change the section
title first (line 342):

```tsx
            <span>{isReference ? "Assembly" : "Parsed facts"}</span>
```

Then replace the truthy branch of that ternary:

```tsx
            <>
              {isReference ? (
                <AssemblyFacts facts={obj.facts} />
              ) : (
                <FactsTable facts={obj.facts} />
              )}
              <div style={{ display: "flex", gap: 24, marginTop: 14, flexWrap: "wrap" }}>
                {Array.isArray(obj.facts.base_composition) && (
                  <div style={{ flex: "0 1 auto" }}>
                    <div
                      style={{
                        fontSize: 11,
                        color: "var(--text-faint)",
                        marginBottom: 6,
                      }}
                    >
                      Base composition
                    </div>
                    <BaseCompositionChart
                      composition={obj.facts.base_composition as never}
                      sampledReads={obj.facts.stats_sampled_reads as number | undefined}
                      sampledBases={obj.facts.stats_sampled_bases as number | undefined}
                      gcPercent={obj.facts.gc_content_percent as number | undefined}
                    />
                  </div>
                )}
                {/* A FASTA carries no per-base qualities, so the quality curve
                    is meaningless for a reference. */}
                {!isReference && Array.isArray(obj.facts.quality_per_position) && (
                  <div style={{ flex: "1 1 auto", minWidth: 300 }}>
                    <div
                      style={{
                        fontSize: 11,
                        color: "var(--text-faint)",
                        marginBottom: 6,
                      }}
                    >
                      Quality per position
                    </div>
                    <QualityChart curve={obj.facts.quality_per_position as never} />
                  </div>
                )}
              </div>
            </>
```

The base-composition chart is deliberately kept for references — GC skew and
N-fraction are informative for an assembly.

- [ ] **Step 4: Hide the SRA panel for references**

Replace line 441:

```tsx
        {/* SRA run/experiment accessions are the wrong archive for an
            assembly; assembly_accession links to NCBI Datasets instead. */}
        {!isReference && <SraPanel facts={obj.facts} formatKind={obj.format.kind} />}
```

- [ ] **Step 5: Add the converter above Delete**

Immediately before the `<div className="section">` that contains the Delete
title (line 474):

```tsx
        <RoleConverter obj={obj} />
```

- [ ] **Step 6: Verify it compiles**

Run: `npm --prefix frontend run lint`

Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/DetailPanel.tsx
git commit -m "feat: render reference-specific detail panel"
```

---

## Task 13: End-to-end verification

No code in this task. Confirm the feature works against a running stack before
calling it done.

- [ ] **Step 1: Rebuild and start**

```bash
make up
```

Expected: `READY`

- [ ] **Step 2: Run the full backend suite**

```bash
make test
```

Expected: all pass, no errors.

- [ ] **Step 3: Lint**

```bash
make lint
```

Expected: no findings. Fix anything reported.

- [ ] **Step 4: Walk the happy path in the browser**

Open http://localhost:5173, then with a project containing a FASTA or FASTQ:

1. Click a reads file. Confirm the panel header says **File**, and the Role
   section offers "Convert to reference".
2. Click Convert. Confirm: a success toast; the file disappears from **Reads**
   and appears under **References** with a 📗 icon; the header now says
   **Reference**.
3. Confirm the metadata form now shows Build, Source, Assembly accession,
   Primary assembly only, Includes decoy contigs, Aligner indexes — and no
   Library or Sequencing group.
4. Confirm the facts section is titled **Assembly** and shows sequence count,
   total bases, and GC content labelled "(sampled)".
5. Confirm the SRA panel is gone and no quality-per-position chart renders.
6. Type `GCA_000001405.29` into Assembly accession, save, and confirm an
   "Assembly ↗" link appears beside the label and resolves at NCBI.

- [ ] **Step 5: Verify reversibility preserves metadata**

1. Convert back to reads. Confirm the file returns to **Reads** with a 📄 icon
   and the header reads **File** again.
2. Confirm the Library and Sequencing groups are back.
3. Confirm the assembly values entered above are not lost — they should appear
   under **Custom fields** in the editor.
4. Convert to reference once more and confirm they return to their proper
   labelled inputs.

This round trip is the single most important manual check: it proves no
metadata is destroyed by conversion.

- [ ] **Step 6: Confirm non-convertible files show no control**

Select a BAM or VCF (if the project has one). Confirm there is no Role section.

- [ ] **Step 7: Commit any fixes**

```bash
git add -A
git commit -m "fix: address issues found in end-to-end verification"
```

Skip this step if nothing needed fixing.

---

## Self-review notes

Checked against the spec:

- **Data model** — Task 1 (enum, field, index).
- **API** — Tasks 2, 3, 5. The three PATCH cases the spec called for are covered
  by `TestApplyRoleUpdate` at the service layer rather than through HTTP:
  `tests/api/` contains only `__init__.py`, so no API test harness exists, and
  standing one up is out of proportion to this change. The logic under test is
  identical; only the transport is unexercised, and Step 2 of Task 5 checks that
  by hand.
- **Metadata schema** — Task 4, including the `FASTA → ()` change and the
  survival-across-conversion tests the spec explicitly asked for.
- **Reference fields** — Task 4, all seven with the specified types and
  `suggested` flags.
- **Left panel** — Task 7.
- **Detail panel** — Tasks 10, 11, 12: Assembly section, sampled-GC labelling,
  no longest/shortest row, kept base composition, dropped quality chart, hidden
  SraPanel, accession link (Task 9), conversion control.
- **Frontend role in the query key** — Task 8.
- **Out of scope, correctly absent:** WIG roles, per-sequence lengths, bulk
  conversion, search filtering beyond the index.

One spec item deliberately altered: the spec put role coercion in
`update_object` without specifying ordering. Task 3 Step 4 pins role to be
applied *before* metadata, so a single PATCH carrying both fields coerces the
metadata against the incoming role rather than the outgoing one.
