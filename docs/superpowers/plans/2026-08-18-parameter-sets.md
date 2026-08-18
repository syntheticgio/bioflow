# Pipeline Parameter Sets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user save a pipeline run's tuning parameters as a named, per-profile set and re-apply it when configuring a later run of the same tool, with drift and validation surfaced on apply and the applied set recorded on the run.

**Architecture:** A new `ParameterSet` Beanie document stores tuning knobs only, keyed by `(owner, tool)`. Which keys are eligible, and whether a saved key still validates, are both *derived* from the tool's existing `ParamField` metadata in `aligner_registry` / `assembler_registry` — so there is no hand-maintained allowlist and the drift check and the validation check are one comparison. A `POST /{id}/resolve` endpoint does that comparison server-side and returns applied values plus per-key rejections; the dialog fills fields from `applied` and renders a persistent notice from `rejected`.

**Tech Stack:** Python 3.12, FastAPI, Beanie/Motor (MongoDB), Pydantic v2, pytest; React + TypeScript, TanStack Query, Vite.

**Spec:** [`docs/superpowers/specs/2026-08-18-parameter-sets-design.md`](../specs/2026-08-18-parameter-sets-design.md)

## Global Constraints

- **Name the type `ParameterSet`, never `Preset`.** `align_runner.Preset` and `AlignerSpec.presets` already mean two other things in these modules. User-facing UI copy still says "preset".
- **v1 covers aligners and assemblers only.** These are the two families with a declarative `ParamSpec`. No other tool gets preset UI.
- **Eligible keys are derived from `spec.fields`, never hand-listed.** A hand-maintained per-tool key dict is the silent-skip registry shape CLAUDE.md warns about.
- **`params_sanitizer.sanitize()` must NOT be called anywhere on the parameter-set path.** It is a disclosure boundary for uploaded computation records, not input validation. Task 4 adds a regression test asserting this.
- **A set stores tuning knobs only** — never `reference_id`, input object ids, `target_node`, `project_id`, `label`, `chunked`, or read-group overrides.
- **`revision` bumps on a params edit, never on a rename.**
- **Nothing auto-applies.** No default sets, no per-project defaults.
- **Run tests from the worktree with `./backend/run-worktree-tests.sh`**, never `docker compose exec api` — the latter silently tests main's code.
- **Conventional Commits**, lowercase after the colon, no trailing period. Enable the hook once per checkout: `git config core.hooksPath ops/hooks`.

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/app/models/parameter_set.py` | **Create.** `ParamSpecFamily` enum, `ParameterSet` document, indexes. |
| `backend/app/models/__init__.py` | **Modify.** Register `ParameterSet` in `ALL_MODELS` (line ~75) and `__all__` (line ~110). |
| `backend/app/services/parameter_set_service.py` | **Create.** `spec_fields`, `preset_eligible_keys`, `eligible_params`, `resolve` — all pure, no HTTP. |
| `backend/app/api/v1/parameter_sets.py` | **Create.** CRUD + `resolve` routes, all `OwnerDep`-scoped. |
| `backend/app/api/v1/__init__.py` | **Modify.** `include_router(parameter_sets.router)` (after line 55). |
| `backend/app/models/run.py` | **Modify.** `AppliedParameterSet` model + `from_parameter_set` field on `PipelineRun` (line ~117). |
| `backend/app/services/run_service.py` | **Modify.** `create_run` gains `from_parameter_set` kwarg (line 73). |
| `backend/tests/services/test_parameter_set_service.py` | **Create.** Derivation, eligibility, four rejection reasons, sanitize-not-called. |
| `backend/tests/api/test_parameter_sets.py` | **Create.** CRUD, uniqueness, owner isolation, resolve envelope. |
| `backend/tests/models/test_parameter_set_provenance.py` | **Create.** Provenance snapshot survives rename. |
| `frontend/src/api/types.ts` | **Modify.** `ParameterSet`, `ResolveResult`, `RejectedParam` types. |
| `frontend/src/api.ts` | **Modify.** Five client methods. |
| `frontend/src/components/ParameterSetPicker.tsx` | **Create.** Picker row, save/rename/delete, persistent drift notice. |
| `frontend/src/components/AlignDialog.tsx` | **Modify.** Mount picker; thread applied-set context into launch. |
| `frontend/src/components/AssembleDialog.tsx` | **Modify.** Same. |

---

## Task 1: The `ParameterSet` model

**Files:**
- Create: `backend/app/models/parameter_set.py`
- Modify: `backend/app/models/__init__.py`
- Test: `backend/tests/models/test_parameter_set_model.py`

**Interfaces:**
- Consumes: `app.models.base.TimestampedDocument`.
- Produces: `ParamSpecFamily` (StrEnum: `ALIGNER = "aligner"`, `ASSEMBLER = "assembler"`); `ParameterSet(TimestampedDocument)` with fields `name: str`, `tool: str`, `family: ParamSpecFamily`, `params: dict`, `revision: int = 1`. Collection name `parameter_sets`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/models/test_parameter_set_model.py`:

```python
"""The ParameterSet document (#414)."""

import pytest

from app.models.parameter_set import ParameterSet, ParamSpecFamily


class TestParameterSetModel:
    def test_defaults_to_revision_one(self):
        s = ParameterSet(
            name="Nanopore fast",
            tool="minimap2",
            family=ParamSpecFamily.ALIGNER,
            params={"threads": 8},
        )
        assert s.revision == 1

    def test_family_is_a_string_enum(self):
        assert ParamSpecFamily.ALIGNER == "aligner"
        assert ParamSpecFamily.ASSEMBLER == "assembler"

    def test_collection_name(self):
        assert ParameterSet.Settings.name == "parameter_sets"

    def test_declares_unique_name_per_owner_and_tool(self):
        names = {i.document["name"] for i in ParameterSet.Settings.indexes}
        assert "owner_tool_name_unique" in names

    def test_is_registered_for_beanie(self):
        from app.models import ALL_MODELS

        assert ParameterSet in ALL_MODELS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/models/test_parameter_set_model.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.models.parameter_set'`

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/models/parameter_set.py`:

```python
"""Saved pipeline parameter sets: named tuning knobs a user can re-apply.

Named `ParameterSet` rather than `Preset` because "preset" already means two
other things in this subsystem: `align_runner.Preset` (minimap2's `-x` values)
and `AlignerSpec.presets` (built-in read-only tuning profiles). Both are
tool-authored and immutable; this one is user-authored and per-profile. The UI
still says "preset" -- users never see the type name.

See docs/superpowers/specs/2026-08-18-parameter-sets-design.md.
"""

from enum import StrEnum

from pymongo import ASCENDING, IndexModel

from app.models.base import TimestampedDocument


class ParamSpecFamily(StrEnum):
    """Which registry resolves a set's `tool` to its `ParamField` list.

    Stored rather than inferred: `aligner_registry.spec_for` and
    `assembler_registry.spec_for` are separate lookups whose schema envelopes
    differ, and guessing which one owns a tool string at apply time is a
    lookup that fails silently when a name is ambiguous.

    Grows by one member per tool family that gains a `ParamSpec`.
    """

    ALIGNER = "aligner"
    ASSEMBLER = "assembler"


class ParameterSet(TimestampedDocument):
    """Tuning knobs a user saved under a name, scoped to one specific tool.

    Bound to the tool rather than the run kind: minimap2 and STAR parameters
    are not interchangeable, so a kind-scoped set would be almost entirely
    drift by construction.

    `params` holds tuning knobs only -- never input bindings. That exclusion is
    structural rather than a denylist: `parameter_set_service` derives the
    eligible keys from the tool's `spec.fields`, and `reference_id`, input
    object ids, `target_node`, and `project_id` are excluded because they were
    never `ParamField`s.
    """

    name: str
    tool: str
    family: ParamSpecFamily
    params: dict = {}
    # Bumped when `params` changes, never on rename. Provenance asks "were
    # these runs configured the same?" -- a rename does not change that
    # answer, an edit does.
    revision: int = 1

    class Settings:
        name = "parameter_sets"
        indexes = [
            IndexModel(
                [("owner", ASCENDING), ("tool", ASCENDING)],
                name="owner_tool",
            ),
            # Enforced at the database rather than in a service check that
            # races two concurrent saves of the same name.
            IndexModel(
                [("owner", ASCENDING), ("tool", ASCENDING), ("name", ASCENDING)],
                name="owner_tool_name_unique",
                unique=True,
            ),
        ]
```

Then in `backend/app/models/__init__.py`, add the import alongside the others (alphabetical, after `local_database`):

```python
from app.models.parameter_set import ParameterSet, ParamSpecFamily
```

add `ParameterSet` to the `ALL_MODELS` list, and add `"ParameterSet"` and `"ParamSpecFamily"` to `__all__`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/models/test_parameter_set_model.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/parameter_set.py backend/app/models/__init__.py backend/tests/models/test_parameter_set_model.py
git commit -m "feat(models): add a ParameterSet document for saved pipeline knobs"
```

---

## Task 2: Deriving eligible keys from the tool's spec

**Files:**
- Create: `backend/app/services/parameter_set_service.py`
- Test: `backend/tests/services/test_parameter_set_service.py`

**Interfaces:**
- Consumes: `ParamSpecFamily` from Task 1; `aligner_registry.spec_for(Aligner)`, `assembler_registry.spec_for(Assembler)`, both returning a spec with a `.fields: tuple[ParamField, ...]`; `ParamField` has `key`, `kind` (`"int"|"bool"|"select"|"text"`), `default`, `min`, `max`, `choices: tuple[Choice, ...]` where `Choice` has `.value`.
- Produces: `spec_fields(family, tool) -> tuple[ParamField, ...]`; `preset_eligible_keys(family, tool) -> frozenset[str]`; `eligible_params(family, tool, params) -> dict`; `has_parameter_sets(family, tool) -> bool`; `UnknownToolError(ValueError)`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/test_parameter_set_service.py`:

```python
"""Deriving parameter-set eligibility from tool specs (#414).

The point of every test here is that nothing is hand-listed: eligibility comes
from `spec.fields`, so a knob added to a registry becomes saveable with no
second edit.
"""

import pytest

from app.models.parameter_set import ParamSpecFamily
from app.pipelines.aligners import Aligner
from app.pipelines.assemblers import Assembler
from app.services import parameter_set_service as svc


class TestSpecFields:
    def test_resolves_an_aligner(self):
        fields = svc.spec_fields(ParamSpecFamily.ALIGNER, Aligner.MINIMAP2.value)
        assert fields
        assert all(hasattr(f, "key") for f in fields)

    def test_resolves_an_assembler(self):
        # FLYE and ABYSS declare fields; HIFIASM and SPADES do not (yet).
        fields = svc.spec_fields(ParamSpecFamily.ASSEMBLER, Assembler.FLYE.value)
        assert fields
        assert all(hasattr(f, "key") for f in fields)

    def test_a_spec_with_no_fields_resolves_to_empty(self):
        """`AssemblerSpec.fields` defaults to `()`. HIFIASM and SPADES take
        that default today, so they resolve without raising and yield no
        eligible keys -- which is what `has_parameter_sets` below exists to
        detect, so the UI never offers a picker that can only save nothing."""
        assert svc.spec_fields(ParamSpecFamily.ASSEMBLER, Assembler.SPADES.value) == ()

    def test_unknown_tool_raises(self):
        with pytest.raises(svc.UnknownToolError):
            svc.spec_fields(ParamSpecFamily.ALIGNER, "not-a-real-aligner")


class TestEligibleKeys:
    def test_keys_come_from_the_spec(self):
        expected = {
            f.key for f in svc.spec_fields(ParamSpecFamily.ALIGNER, Aligner.MINIMAP2.value)
        }
        assert svc.preset_eligible_keys(ParamSpecFamily.ALIGNER, Aligner.MINIMAP2.value) == expected


class TestEligibleParams:
    def test_drops_input_bindings(self):
        keys = svc.preset_eligible_keys(ParamSpecFamily.ALIGNER, Aligner.MINIMAP2.value)
        knob = next(iter(keys))
        params = {
            knob: 8,
            "reference_id": "68a1f00000000000000000aa",
            "target_node": "worker-2",
            "project_id": "68a1f00000000000000000bb",
            "label": "sample -> ref",
            "chunked": True,
        }
        got = svc.eligible_params(ParamSpecFamily.ALIGNER, Aligner.MINIMAP2.value, params)
        assert got == {knob: 8}

    def test_empty_params_is_empty(self):
        assert svc.eligible_params(ParamSpecFamily.ALIGNER, Aligner.MINIMAP2.value, {}) == {}


class TestHasParameterSets:
    def test_true_when_the_spec_declares_fields(self):
        assert svc.has_parameter_sets(ParamSpecFamily.ALIGNER, Aligner.MINIMAP2.value)

    def test_false_when_the_spec_declares_none(self):
        assert not svc.has_parameter_sets(ParamSpecFamily.ASSEMBLER, Assembler.SPADES.value)

    def test_false_for_an_unknown_tool(self):
        assert not svc.has_parameter_sets(ParamSpecFamily.ALIGNER, "not-a-real-aligner")


class TestEveryToolIsResolvable:
    """Exhaustiveness, in the shape CLAUDE.md's derivable-registry section
    prescribes: every member of both enums resolves, so a tool added to a
    registry cannot silently have no eligible keys."""

    @pytest.mark.parametrize("aligner", list(Aligner))
    def test_every_aligner_resolves(self, aligner):
        svc.spec_fields(ParamSpecFamily.ALIGNER, aligner.value)

    @pytest.mark.parametrize("assembler", list(Assembler))
    def test_every_assembler_resolves(self, assembler):
        svc.spec_fields(ParamSpecFamily.ASSEMBLER, assembler.value)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_parameter_set_service.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.parameter_set_service'`

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/services/parameter_set_service.py`:

```python
"""What a parameter set may hold, and whether a saved value still applies.

Everything here derives from the tool's own `ParamField` metadata. Nothing is
hand-listed, which is the whole point: a hand-maintained per-tool key dict is
the silent-skip registry shape CLAUDE.md's "Hand-maintained registries keyed by
an enum" section warns about -- a missing entry would mean "no keys eligible",
producing a set that saves and applies nothing with no error anywhere.

`params_sanitizer.sanitize()` is deliberately NOT called from this module. It
is a disclosure boundary deciding what is safe to upload in computation
records, not input validation -- freshly typed parameters never pass through
it either. Its fourteen-key allowlist rejects any value containing a path
separator, so applying it here would strip most real parameters and produce
exactly the silent under-application the drift notice exists to prevent. See
the spec's decision 6.
"""

from typing import Any

from app.models.parameter_set import ParamSpecFamily
from app.pipelines import aligner_registry, assembler_registry
from app.pipelines.aligner_registry import ParamField
from app.pipelines.aligners import Aligner
from app.pipelines.assemblers import Assembler


class UnknownToolError(ValueError):
    """A set names a tool its family's registry does not know."""


def spec_fields(family: ParamSpecFamily, tool: str) -> tuple[ParamField, ...]:
    """The tool's declared parameter fields, whichever registry owns it."""
    try:
        if family is ParamSpecFamily.ALIGNER:
            return tuple(aligner_registry.spec_for(Aligner(tool)).fields)
        return tuple(assembler_registry.spec_for(Assembler(tool)).fields)
    except (ValueError, KeyError) as exc:
        raise UnknownToolError(f"{family.value} {tool!r} is not registered") from exc


def preset_eligible_keys(family: ParamSpecFamily, tool: str) -> frozenset[str]:
    """Which param keys a set for this tool may hold.

    Derived, so a field added to a spec becomes saveable without a second
    edit here -- and `reference_id`, input object ids, `target_node`,
    `project_id`, `label`, and `chunked` are excluded structurally, because
    they were never `ParamField`s.
    """
    return frozenset(f.key for f in spec_fields(family, tool))


def eligible_params(
    family: ParamSpecFamily, tool: str, params: dict[str, Any]
) -> dict[str, Any]:
    """`params` narrowed to what a set for this tool may store."""
    keys = preset_eligible_keys(family, tool)
    return {k: v for k, v in params.items() if k in keys}


def has_parameter_sets(family: ParamSpecFamily, tool: str) -> bool:
    """Whether this tool has enough declared parameters to be worth saving.

    `AssemblerSpec.fields` defaults to `()`, and HIFIASM and SPADES take that
    default today. Such a tool resolves without raising but has no eligible
    keys, so a set saved against it would store nothing and apply nothing --
    silently. The UI asks this before rendering a picker, so a tool that can
    only save an empty set never offers to.
    """
    try:
        return bool(spec_fields(family, tool))
    except UnknownToolError:
        return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/services/test_parameter_set_service.py -v`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/parameter_set_service.py backend/tests/services/test_parameter_set_service.py
git commit -m "feat(pipelines): derive parameter-set eligible keys from tool specs"
```

---

## Task 3: The resolve comparison and its four rejection reasons

**Files:**
- Modify: `backend/app/services/parameter_set_service.py`
- Test: `backend/tests/services/test_parameter_set_service.py` (append)

**Interfaces:**
- Consumes: `spec_fields` from Task 2.
- Produces: `RejectionReason` (StrEnum: `UNKNOWN_FIELD = "unknown_field"`, `WRONG_KIND = "wrong_kind"`, `OUT_OF_RANGE = "out_of_range"`, `INVALID_CHOICE = "invalid_choice"`); `Rejected` (Pydantic: `key: str`, `reason: RejectionReason`, `detail: str`, `value: Any`); `Resolution` (Pydantic: `applied: dict`, `rejected: list[Rejected]`); `resolve_params(family, tool, params) -> Resolution`.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/services/test_parameter_set_service.py`:

```python
from dataclasses import replace

from app.pipelines.aligner_registry import Choice, ParamField


def _int_field(**kw):
    base = dict(key="threads", label="Threads", kind="int", default=4, help="")
    return ParamField(**{**base, **kw})


class TestResolveRejectionReasons:
    """One test per reason in the spec's four-reason table. Each mutates a
    field the way a tool's spec would drift between save and apply."""

    def test_unknown_field(self, monkeypatch):
        monkeypatch.setattr(svc, "spec_fields", lambda f, t: (_int_field(),))
        got = svc.resolve_params(
            ParamSpecFamily.ALIGNER, "minimap2", {"threads": 8, "gone": 1}
        )
        assert got.applied == {"threads": 8}
        assert [r.reason for r in got.rejected] == [svc.RejectionReason.UNKNOWN_FIELD]
        assert got.rejected[0].key == "gone"

    def test_wrong_kind(self, monkeypatch):
        monkeypatch.setattr(svc, "spec_fields", lambda f, t: (_int_field(),))
        got = svc.resolve_params(ParamSpecFamily.ALIGNER, "minimap2", {"threads": "eight"})
        assert got.applied == {}
        assert got.rejected[0].reason is svc.RejectionReason.WRONG_KIND

    def test_out_of_range(self, monkeypatch):
        monkeypatch.setattr(svc, "spec_fields", lambda f, t: (_int_field(min=1, max=12),))
        got = svc.resolve_params(ParamSpecFamily.ALIGNER, "minimap2", {"threads": 16})
        assert got.applied == {}
        assert got.rejected[0].reason is svc.RejectionReason.OUT_OF_RANGE
        assert "12" in got.rejected[0].detail

    def test_invalid_choice(self, monkeypatch):
        field = ParamField(
            key="preset", label="Preset", kind="select", default="sr", help="",
            choices=(Choice(value="sr", label="Short read"),),
        )
        monkeypatch.setattr(svc, "spec_fields", lambda f, t: (field,))
        got = svc.resolve_params(ParamSpecFamily.ALIGNER, "minimap2", {"preset": "map-ont"})
        assert got.applied == {}
        assert got.rejected[0].reason is svc.RejectionReason.INVALID_CHOICE

    def test_bool_accepts_only_bool(self, monkeypatch):
        field = ParamField(key="paired", label="Paired", kind="bool", default=True, help="")
        monkeypatch.setattr(svc, "spec_fields", lambda f, t: (field,))
        assert svc.resolve_params(ParamSpecFamily.ALIGNER, "minimap2", {"paired": False}).applied == {"paired": False}
        assert svc.resolve_params(ParamSpecFamily.ALIGNER, "minimap2", {"paired": 1}).rejected


class TestResolveDoesNotSanitize:
    """Regression guard for the spec's decision 6.

    The issue's question 5 asks for `params_sanitizer.sanitize()` on apply.
    That module is a disclosure boundary for uploaded records, not input
    validation; applying it here would strip every value containing a path
    separator and every key outside its fourteen-key allowlist. If someone
    "fixes" this back, this test fails."""

    def test_keeps_values_sanitize_would_strip(self, monkeypatch):
        field = ParamField(key="extra_args", label="Extra", kind="text", default="", help="")
        monkeypatch.setattr(svc, "spec_fields", lambda f, t: (field,))
        got = svc.resolve_params(
            ParamSpecFamily.ALIGNER, "minimap2", {"extra_args": "--junc-bed=/data/ref.bed"}
        )
        assert got.applied == {"extra_args": "--junc-bed=/data/ref.bed"}
        assert got.rejected == []

    def test_sanitize_is_not_imported_by_this_module(self):
        import inspect

        assert "params_sanitizer" not in inspect.getsource(svc)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_parameter_set_service.py -v -k "Rejection or Sanitize"`
Expected: FAIL — `AttributeError: module 'app.services.parameter_set_service' has no attribute 'resolve_params'`

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/services/parameter_set_service.py` (add `from enum import StrEnum` and `from pydantic import BaseModel` to the imports):

```python
class RejectionReason(StrEnum):
    """Why a saved value did not reach the form.

    Each member is derived from a `ParamField` attribute rather than a
    hand-written rule, which is what makes the drift check and the validation
    check the same comparison instead of two rules to keep in sync.
    """

    UNKNOWN_FIELD = "unknown_field"    # key not in spec.fields
    WRONG_KIND = "wrong_kind"          # field.kind
    OUT_OF_RANGE = "out_of_range"      # field.min / field.max
    INVALID_CHOICE = "invalid_choice"  # field.choices


class Rejected(BaseModel):
    key: str
    reason: RejectionReason
    detail: str
    value: Any = None


class Resolution(BaseModel):
    applied: dict[str, Any] = {}
    rejected: list[Rejected] = []


def _check(field: ParamField, value: Any) -> Rejected | None:
    """The one comparison. `None` means the value still applies."""
    if field.kind == "bool":
        if not isinstance(value, bool):
            return Rejected(
                key=field.key, reason=RejectionReason.WRONG_KIND, value=value,
                detail=f"{field.label} expects true or false",
            )
        return None

    if field.kind == "int":
        # bool is an int subclass in Python; a checkbox value is not a number.
        if isinstance(value, bool) or not isinstance(value, int):
            return Rejected(
                key=field.key, reason=RejectionReason.WRONG_KIND, value=value,
                detail=f"{field.label} expects a whole number",
            )
        if field.min is not None and value < field.min:
            return Rejected(
                key=field.key, reason=RejectionReason.OUT_OF_RANGE, value=value,
                detail=f"{value} is below the current minimum of {field.min}",
            )
        if field.max is not None and value > field.max:
            return Rejected(
                key=field.key, reason=RejectionReason.OUT_OF_RANGE, value=value,
                detail=f"{value} exceeds the current maximum of {field.max}",
            )
        return None

    if not isinstance(value, str):
        return Rejected(
            key=field.key, reason=RejectionReason.WRONG_KIND, value=value,
            detail=f"{field.label} expects text",
        )
    if field.kind == "select":
        allowed = {c.value for c in field.choices}
        if value not in allowed:
            return Rejected(
                key=field.key, reason=RejectionReason.INVALID_CHOICE, value=value,
                detail=f"{value!r} is no longer an option for {field.label}",
            )
    return None


def resolve_params(
    family: ParamSpecFamily, tool: str, params: dict[str, Any]
) -> Resolution:
    """Split a set's saved params into what still applies and what does not.

    Applies what matches and reports the rest rather than refusing outright:
    one removed parameter should not make an otherwise-good set useless. See
    the spec's decision 1.
    """
    by_key = {f.key: f for f in spec_fields(family, tool)}
    applied: dict[str, Any] = {}
    rejected: list[Rejected] = []

    for key, value in params.items():
        field = by_key.get(key)
        if field is None:
            rejected.append(
                Rejected(
                    key=key, reason=RejectionReason.UNKNOWN_FIELD, value=value,
                    detail=f"no longer a parameter of {tool}",
                )
            )
            continue
        problem = _check(field, value)
        if problem is None:
            applied[key] = value
        else:
            rejected.append(problem)

    return Resolution(applied=applied, rejected=rejected)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/services/test_parameter_set_service.py -v`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/parameter_set_service.py backend/tests/services/test_parameter_set_service.py
git commit -m "feat(pipelines): resolve saved params against a tool's current schema"
```

---

## Task 4: The CRUD and resolve API

**Files:**
- Create: `backend/app/api/v1/parameter_sets.py`
- Modify: `backend/app/api/v1/__init__.py`
- Test: `backend/tests/api/test_parameter_sets.py`

**Interfaces:**
- Consumes: `ParameterSet`, `ParamSpecFamily` (Task 1); `eligible_params`, `resolve_params`, `UnknownToolError` (Tasks 2-3); `OwnerDep` from `app.api.deps`.
- Produces: routes under `/api/v1/parameter-sets`. Response shape `ParameterSetOut{id, name, tool, family, params, revision}`; resolve returns `{applied, rejected, set}`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/api/test_parameter_sets.py`:

```python
"""The parameter-set API (#414)."""

import pytest

from app.models.parameter_set import ParameterSet

pytestmark = pytest.mark.anyio


async def _create(client, headers, **kw):
    body = {
        "name": "Nanopore fast",
        "tool": "minimap2",
        "family": "aligner",
        "params": {"threads": 8},
    }
    body.update(kw)
    return await client.post("/api/v1/parameter-sets", json=body, headers=headers)


class TestCrud:
    async def test_create_then_list_by_tool(self, client, two_profiles):
        a, _ = two_profiles
        r = await _create(client, a.headers)
        assert r.status_code == 201, r.text
        assert r.json()["revision"] == 1

        listed = await client.get(
            "/api/v1/parameter-sets", params={"tool": "minimap2"}, headers=a.headers
        )
        assert [s["name"] for s in listed.json()] == ["Nanopore fast"]

    async def test_list_requires_a_tool(self, client, two_profiles):
        a, _ = two_profiles
        r = await client.get("/api/v1/parameter-sets", headers=a.headers)
        assert r.status_code == 422

    async def test_rename_keeps_revision(self, client, two_profiles):
        a, _ = two_profiles
        sid = (await _create(client, a.headers)).json()["id"]
        r = await client.patch(
            f"/api/v1/parameter-sets/{sid}", json={"name": "Renamed"}, headers=a.headers
        )
        assert r.json() == {**r.json(), "name": "Renamed", "revision": 1}

    async def test_editing_params_bumps_revision(self, client, two_profiles):
        a, _ = two_profiles
        sid = (await _create(client, a.headers)).json()["id"]
        r = await client.patch(
            f"/api/v1/parameter-sets/{sid}",
            json={"params": {"threads": 12}},
            headers=a.headers,
        )
        assert r.json()["revision"] == 2

    async def test_delete(self, client, two_profiles):
        a, _ = two_profiles
        sid = (await _create(client, a.headers)).json()["id"]
        assert (await client.delete(f"/api/v1/parameter-sets/{sid}", headers=a.headers)).status_code == 204
        listed = await client.get(
            "/api/v1/parameter-sets", params={"tool": "minimap2"}, headers=a.headers
        )
        assert listed.json() == []


class TestSaveDropsIneligibleKeys:
    async def test_input_bindings_are_not_stored(self, client, two_profiles):
        a, _ = two_profiles
        r = await _create(
            client, a.headers,
            params={"threads": 8, "reference_id": "68a1f00000000000000000aa", "chunked": True},
        )
        assert r.json()["params"] == {"threads": 8}


class TestUniqueness:
    async def test_same_name_same_tool_collides(self, client, two_profiles):
        a, _ = two_profiles
        await _create(client, a.headers)
        assert (await _create(client, a.headers)).status_code == 409

    async def test_same_name_different_tool_is_fine(self, client, two_profiles):
        a, _ = two_profiles
        await _create(client, a.headers)
        r = await _create(client, a.headers, tool="star", params={})
        assert r.status_code == 201


class TestOwnerIsolation:
    async def test_b_cannot_list_as_own(self, client, two_profiles):
        a, b = two_profiles
        await _create(client, a.headers)
        r = await client.get(
            "/api/v1/parameter-sets", params={"tool": "minimap2"}, headers=b.headers
        )
        assert r.json() == []

    async def test_b_cannot_delete_as_own(self, client, two_profiles):
        a, b = two_profiles
        sid = (await _create(client, a.headers)).json()["id"]
        assert (await client.delete(f"/api/v1/parameter-sets/{sid}", headers=b.headers)).status_code == 404


class TestSupported:
    async def test_true_for_a_tool_with_fields(self, client, two_profiles):
        a, _ = two_profiles
        r = await client.get(
            "/api/v1/parameter-sets/supported",
            params={"family": "aligner", "tool": "minimap2"},
            headers=a.headers,
        )
        assert r.json() == {"supported": True}

    async def test_false_for_a_tool_without_fields(self, client, two_profiles):
        a, _ = two_profiles
        r = await client.get(
            "/api/v1/parameter-sets/supported",
            params={"family": "assembler", "tool": "spades"},
            headers=a.headers,
        )
        assert r.json() == {"supported": False}


class TestResolve:
    async def test_returns_applied_and_set_identity(self, client, two_profiles):
        a, _ = two_profiles
        created = (await _create(client, a.headers)).json()
        r = await client.post(
            f"/api/v1/parameter-sets/{created['id']}/resolve", headers=a.headers
        )
        body = r.json()
        assert body["applied"]["threads"] == 8
        assert body["rejected"] == []
        assert body["set"] == {"id": created["id"], "name": "Nanopore fast", "revision": 1}

    async def test_flags_a_drifted_key(self, client, two_profiles):
        a, _ = two_profiles
        created = (await _create(client, a.headers)).json()
        await ParameterSet.find_one(ParameterSet.id == created["id"]).update(
            {"$set": {"params": {"threads": 8, "gone": 1}}}
        )
        body = (await client.post(
            f"/api/v1/parameter-sets/{created['id']}/resolve", headers=a.headers
        )).json()
        assert body["applied"] == {"threads": 8}
        assert body["rejected"][0]["key"] == "gone"
        assert body["rejected"][0]["reason"] == "unknown_field"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/api/test_parameter_sets.py -v`
Expected: FAIL — every request 404s; the router does not exist.

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/api/v1/parameter_sets.py`:

```python
"""Saved pipeline parameter sets.

Every route is `OwnerDep`-scoped: a set belongs to the profile that saved it,
the same partitioning every other collection uses.

`?tool=` is required on list rather than optional. An optional filter would
create a route returning every set across every tool, which is the route a
cross-tool picker would later be built on -- quietly undoing the decision to
bind a set to one specific tool.
"""

from beanie import PydanticObjectId
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from pymongo.errors import DuplicateKeyError

from app.api.deps import OwnerDep
from app.models.parameter_set import ParameterSet, ParamSpecFamily
from app.services import parameter_set_service as svc

router = APIRouter(prefix="/parameter-sets", tags=["parameter-sets"])


class ParameterSetIn(BaseModel):
    name: str
    tool: str
    family: ParamSpecFamily
    params: dict = {}


class ParameterSetUpdate(BaseModel):
    name: str | None = None
    params: dict | None = None


class ParameterSetOut(BaseModel):
    id: str
    name: str
    tool: str
    family: ParamSpecFamily
    params: dict
    revision: int

    @classmethod
    def of(cls, s: ParameterSet) -> "ParameterSetOut":
        return cls(
            id=str(s.id), name=s.name, tool=s.tool,
            family=s.family, params=s.params, revision=s.revision,
        )


async def _owned(set_id: PydanticObjectId, owner: str) -> ParameterSet:
    """A set, or 404. Scoped by owner, so another profile's id reads as absent
    rather than forbidden -- it is not theirs to know about."""
    found = await ParameterSet.find_one(
        ParameterSet.id == set_id, ParameterSet.owner == owner
    )
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Parameter set not found")
    return found


@router.get("", response_model=list[ParameterSetOut])
async def list_parameter_sets(owner: OwnerDep, tool: str = Query(...)) -> list[ParameterSetOut]:
    sets = await ParameterSet.find(
        ParameterSet.owner == owner, ParameterSet.tool == tool
    ).sort(ParameterSet.name).to_list()
    return [ParameterSetOut.of(s) for s in sets]


@router.post("", response_model=ParameterSetOut, status_code=status.HTTP_201_CREATED)
async def create_parameter_set(body: ParameterSetIn, owner: OwnerDep) -> ParameterSetOut:
    try:
        params = svc.eligible_params(body.family, body.tool, body.params)
    except svc.UnknownToolError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    created = ParameterSet(
        name=body.name, tool=body.tool, family=body.family, params=params, owner=owner
    )
    try:
        await created.insert()
    except DuplicateKeyError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"A parameter set named {body.name!r} already exists for {body.tool}",
        ) from exc
    return ParameterSetOut.of(created)


@router.patch("/{set_id}", response_model=ParameterSetOut)
async def update_parameter_set(
    set_id: PydanticObjectId, body: ParameterSetUpdate, owner: OwnerDep
) -> ParameterSetOut:
    found = await _owned(set_id, owner)

    if body.name is not None:
        found.name = body.name
    if body.params is not None:
        # Only a params edit bumps the revision; a rename does not change
        # whether two runs were configured the same way.
        found.params = svc.eligible_params(found.family, found.tool, body.params)
        found.revision += 1

    found.touch()
    try:
        await found.save()
    except DuplicateKeyError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "That name is already taken") from exc
    return ParameterSetOut.of(found)


@router.delete("/{set_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_parameter_set(set_id: PydanticObjectId, owner: OwnerDep) -> None:
    found = await _owned(set_id, owner)
    await found.delete()


@router.get("/supported")
async def tool_supports_parameter_sets(
    family: ParamSpecFamily = Query(...), tool: str = Query(...)
) -> dict:
    """Whether this tool declares enough parameters to be worth saving.

    Not owner-scoped: it is a static property of the registry, the same
    reasoning `aligner_schema` records for itself.
    """
    return {"supported": svc.has_parameter_sets(family, tool)}


@router.post("/{set_id}/resolve")
async def resolve_parameter_set(set_id: PydanticObjectId, owner: OwnerDep) -> dict:
    """What of this set still applies, and why the rest does not.

    A POST that does not mutate: it is server-side because the schema and the
    drift rules are backend truth, and computing the comparison in the dialog
    would mean two implementations of the contract that can disagree.
    """
    found = await _owned(set_id, owner)
    try:
        resolution = svc.resolve_params(found.family, found.tool, found.params)
    except svc.UnknownToolError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    return {
        "applied": resolution.applied,
        "rejected": [r.model_dump() for r in resolution.rejected],
        "set": {"id": str(found.id), "name": found.name, "revision": found.revision},
    }
```

Then in `backend/app/api/v1/__init__.py`: add `parameter_sets` to the module import list and `api_router.include_router(parameter_sets.router)` after line 55.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/api/test_parameter_sets.py -v`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/v1/parameter_sets.py backend/app/api/v1/__init__.py backend/tests/api/test_parameter_sets.py
git commit -m "feat(api): add parameter-set CRUD and an apply-time resolve route"
```

---

## Task 5: Recording the applied set on a run

**Files:**
- Modify: `backend/app/models/run.py`, `backend/app/services/run_service.py:73-94`
- Test: `backend/tests/models/test_parameter_set_provenance.py`

**Interfaces:**
- Consumes: nothing from earlier tasks at runtime — the snapshot is denormalized by design.
- Produces: `AppliedParameterSet(BaseModel)` with `set_id: PydanticObjectId`, `name: str`, `revision: int`, `edited_after_apply: bool`; `PipelineRun.from_parameter_set: AppliedParameterSet | None = None`; `create_run(..., from_parameter_set=None)`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/models/test_parameter_set_provenance.py`:

```python
"""A run remembers which parameter set configured it (#414)."""

import pytest
from beanie import PydanticObjectId

from app.models.run import AppliedParameterSet, PipelineRun, RunKind

pytestmark = pytest.mark.anyio


class TestAppliedParameterSet:
    def test_runs_default_to_no_set(self):
        run = PipelineRun(
            kind=RunKind.ALIGNMENT, project_id=PydanticObjectId(), label="x"
        )
        assert run.from_parameter_set is None

    def test_snapshot_carries_name_and_revision(self):
        applied = AppliedParameterSet(
            set_id=PydanticObjectId(), name="Nanopore fast",
            revision=3, edited_after_apply=True,
        )
        run = PipelineRun(
            kind=RunKind.ALIGNMENT, project_id=PydanticObjectId(),
            label="x", from_parameter_set=applied,
        )
        assert run.from_parameter_set.name == "Nanopore fast"
        assert run.from_parameter_set.revision == 3
        assert run.from_parameter_set.edited_after_apply is True


class TestCreateRunThreadsProvenance:
    async def test_create_run_stores_the_snapshot(self):
        from app.services.run_service import create_run

        applied = AppliedParameterSet(
            set_id=PydanticObjectId(), name="Nanopore fast",
            revision=2, edited_after_apply=False,
        )
        run = await create_run(
            kind=RunKind.ALIGNMENT, project_id=PydanticObjectId(), label="x",
            inputs=[], params={"threads": 8}, owner="owner-prov",
            from_parameter_set=applied,
        )
        reloaded = await PipelineRun.get(run.id)
        assert reloaded.from_parameter_set.name == "Nanopore fast"
        assert reloaded.from_parameter_set.revision == 2

    async def test_absent_by_default(self):
        from app.services.run_service import create_run

        run = await create_run(
            kind=RunKind.ALIGNMENT, project_id=PydanticObjectId(), label="x",
            inputs=[], params={}, owner="owner-prov",
        )
        assert (await PipelineRun.get(run.id)).from_parameter_set is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/models/test_parameter_set_provenance.py -v`
Expected: FAIL — `ImportError: cannot import name 'AppliedParameterSet'`

- [ ] **Step 3: Write minimal implementation**

In `backend/app/models/run.py`, add above `class PipelineRun` (line ~117):

```python
class AppliedParameterSet(BaseModel):
    """Which saved parameter set configured this run, as it read at apply time.

    Denormalized for the same reason `params` and the input names are: a set
    can be renamed or deleted, and a run described only by a dangling id stops
    being describable exactly when the question -- "which of these thirty runs
    used the old settings?" -- is worth asking.

    `revision` and `edited_after_apply` are what make that question answerable
    rather than merely groupable. Thirty runs sharing `(set_id, revision)` with
    `edited_after_apply` false were genuinely configured identically; without
    both fields the grouping would look authoritative while meaning nothing.
    """

    set_id: PydanticObjectId
    name: str
    revision: int
    edited_after_apply: bool = False
```

and inside `PipelineRun`, after `tool`:

```python
    # None means the run was configured by hand, which stays the common case.
    from_parameter_set: AppliedParameterSet | None = None
```

In `backend/app/services/run_service.py`, add the keyword to `create_run` (after `tool`) and pass it through to the `PipelineRun(...)` constructor:

```python
    from_parameter_set: AppliedParameterSet | None = None,
```
```python
        from_parameter_set=from_parameter_set,
```

Import `AppliedParameterSet` alongside the existing `run` model imports in `run_service.py`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/models/test_parameter_set_provenance.py -v`
Expected: 4 passed

- [ ] **Step 5: Run the run-service suite for regressions**

Run: `./backend/run-worktree-tests.sh tests/services -q -k run`
Expected: no new failures — `from_parameter_set` is optional, so existing callers are unaffected.

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/run.py backend/app/services/run_service.py backend/tests/models/test_parameter_set_provenance.py
git commit -m "feat(provenance): record which parameter set configured a run"
```

---

## Task 6: Frontend types and API client

**Files:**
- Modify: `frontend/src/api/types.ts`, `frontend/src/api.ts`

**Interfaces:**
- Consumes: the Task 4 routes.
- Produces: types `ParameterSet`, `RejectedParam`, `ResolveResult`; client methods `listParameterSets(tool)`, `createParameterSet(body)`, `updateParameterSet(id, body)`, `deleteParameterSet(id)`, `resolveParameterSet(id)`.

- [ ] **Step 1: Add the types**

In `frontend/src/api/types.ts`:

```typescript
export type ParamSpecFamily = "aligner" | "assembler";

export interface ParameterSet {
  id: string;
  name: string;
  tool: string;
  family: ParamSpecFamily;
  params: Record<string, unknown>;
  revision: number;
}

export type RejectionReason =
  | "unknown_field"
  | "wrong_kind"
  | "out_of_range"
  | "invalid_choice";

export interface RejectedParam {
  key: string;
  reason: RejectionReason;
  detail: string;
  value: unknown;
}

export interface ResolveResult {
  applied: Record<string, unknown>;
  rejected: RejectedParam[];
  set: { id: string; name: string; revision: number };
}
```

- [ ] **Step 2: Add the client methods**

In `frontend/src/api.ts`, following the file's existing request-helper and `profileHeaders()` conventions:

```typescript
  listParameterSets: (tool: string): Promise<ParameterSet[]> =>
    request(`/parameter-sets?tool=${encodeURIComponent(tool)}`),

  createParameterSet: (body: {
    name: string;
    tool: string;
    family: ParamSpecFamily;
    params: Record<string, unknown>;
  }): Promise<ParameterSet> =>
    request("/parameter-sets", { method: "POST", body: JSON.stringify(body) }),

  updateParameterSet: (
    id: string,
    body: { name?: string; params?: Record<string, unknown> },
  ): Promise<ParameterSet> =>
    request(`/parameter-sets/${id}`, { method: "PATCH", body: JSON.stringify(body) }),

  deleteParameterSet: (id: string): Promise<void> =>
    request(`/parameter-sets/${id}`, { method: "DELETE" }),

  resolveParameterSet: (id: string): Promise<ResolveResult> =>
    request(`/parameter-sets/${id}/resolve`, { method: "POST" }),

  parameterSetsSupported: (
    family: ParamSpecFamily,
    tool: string,
  ): Promise<{ supported: boolean }> =>
    request(
      `/parameter-sets/supported?family=${family}&tool=${encodeURIComponent(tool)}`,
    ),
```

- [ ] **Step 3: Verify it compiles**

Run: `cd frontend && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/api.ts
git commit -m "feat(frontend): add parameter-set types and client methods"
```

---

## Task 7: The picker component

**Files:**
- Create: `frontend/src/components/ParameterSetPicker.tsx`
- Modify: `frontend/src/styles.css`

**Interfaces:**
- Consumes: Task 6's client methods and types.
- Produces: `<ParameterSetPicker tool family currentParams onApply onAppliedSetChange />` where `onApply(values: Record<string, unknown>) => void` and `onAppliedSetChange(applied: {setId, name, revision} | null) => void`.

- [ ] **Step 1: Write the component**

Create `frontend/src/components/ParameterSetPicker.tsx`:

```tsx
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "../api";
import type { ParamSpecFamily, RejectedParam } from "../api/types";

/**
 * Pick, save, rename, and delete saved parameter sets for one tool.
 *
 * Applied values are reported through `onApply` rather than written directly,
 * so the dialog stays the single owner of form state and the generated field
 * renderers need no change.
 *
 * The drift notice is deliberately persistent rather than a toast. Batch work
 * means applying the same stale set thirty times, and a notification that
 * disappears after four seconds is one the user stops reading by sample three
 * -- which would satisfy "flag the rest" on paper while failing in practice.
 */
export function ParameterSetPicker({
  tool,
  family,
  currentParams,
  onApply,
  onAppliedSetChange,
}: {
  tool: string;
  family: ParamSpecFamily;
  currentParams: Record<string, unknown>;
  onApply: (values: Record<string, unknown>) => void;
  onAppliedSetChange: (
    applied: { setId: string; name: string; revision: number } | null,
  ) => void;
}) {
  const qc = useQueryClient();
  const [selected, setSelected] = useState("");
  const [rejected, setRejected] = useState<RejectedParam[]>([]);
  const [appliedCount, setAppliedCount] = useState<{ applied: number; total: number } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const { data: support } = useQuery({
    queryKey: ["parameter-sets", "supported", family, tool],
    queryFn: () => api.parameterSetsSupported(family, tool),
    enabled: !!tool,
  });

  const { data: sets = [] } = useQuery({
    queryKey: ["parameter-sets", tool],
    queryFn: () => api.listParameterSets(tool),
    enabled: !!tool && support?.supported === true,
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["parameter-sets", tool] });

  const applyMutation = useMutation({
    mutationFn: (id: string) => api.resolveParameterSet(id),
    onSuccess: (result) => {
      onApply(result.applied);
      onAppliedSetChange({
        setId: result.set.id,
        name: result.set.name,
        revision: result.set.revision,
      });
      setRejected(result.rejected);
      const total = Object.keys(result.applied).length + result.rejected.length;
      setAppliedCount({ applied: Object.keys(result.applied).length, total });
      setNotice(result.set.name);
    },
    onError: () => setError("Could not apply that preset."),
  });

  const saveMutation = useMutation({
    mutationFn: (name: string) =>
      api.createParameterSet({ name, tool, family, params: currentParams }),
    onSuccess: (created) => {
      invalidate();
      setSelected(created.id);
      setError(null);
    },
    onError: (e: unknown) =>
      setError(
        e instanceof Error && e.message.includes("409")
          ? "A preset with that name already exists for this tool."
          : "Could not save that preset.",
      ),
  });

  const renameMutation = useMutation({
    mutationFn: ({ id, name }: { id: string; name: string }) =>
      api.updateParameterSet(id, { name }),
    onSuccess: invalidate,
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.deleteParameterSet(id),
    onSuccess: () => {
      invalidate();
      setSelected("");
      onAppliedSetChange(null);
      setRejected([]);
      setNotice(null);
    },
  });

  function handleSelect(id: string) {
    setSelected(id);
    setRejected([]);
    setNotice(null);
    if (id) applyMutation.mutate(id);
    else onAppliedSetChange(null);
  }

  function handleSave() {
    const name = window.prompt("Save these settings as:");
    if (name?.trim()) saveMutation.mutate(name.trim());
  }

  function handleRename() {
    const current = sets.find((s) => s.id === selected);
    if (!current) return;
    const name = window.prompt("Rename preset:", current.name);
    if (name?.trim()) renameMutation.mutate({ id: selected, name: name.trim() });
  }

  function handleDelete() {
    const current = sets.find((s) => s.id === selected);
    if (current && window.confirm(`Delete preset "${current.name}"?`)) {
      deleteMutation.mutate(selected);
    }
  }

  // A tool whose spec declares no fields can only ever save an empty set.
  // Rendering nothing is better than offering a control that silently does
  // nothing -- HIFIASM and SPADES are in this state today.
  if (support && !support.supported) return null;

  return (
    <div className="preset-picker">
      <label className="preset-row">
        <span>Preset</span>
        <select value={selected} onChange={(e) => handleSelect(e.target.value)}>
          <option value="">Choose…</option>
          {sets.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </select>
        <button type="button" onClick={handleSave}>
          Save current as…
        </button>
        <button type="button" onClick={handleRename} disabled={!selected}>
          Rename
        </button>
        <button type="button" onClick={handleDelete} disabled={!selected}>
          Delete
        </button>
      </label>

      {error && <p className="preset-error">{error}</p>}

      {notice && rejected.length > 0 && (
        <div className="preset-drift" role="status">
          <p>
            Applied “{notice}” — {appliedCount?.applied} of {appliedCount?.total} settings.
          </p>
          <ul>
            {rejected.map((r) => (
              <li key={r.key}>
                <strong>{r.key}</strong> not applied — {r.detail}.
              </li>
            ))}
          </ul>
          <button type="button" onClick={() => { setNotice(null); setRejected([]); }}>
            Dismiss
          </button>
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Add the styles**

Append to `frontend/src/styles.css`:

```css
.preset-picker { margin-bottom: 0.75rem; }
.preset-row { display: flex; align-items: center; gap: 0.5rem; }
.preset-row select { flex: 1; }
.preset-error { color: var(--danger, #c0392b); font-size: 0.85rem; }
.preset-drift {
  margin-top: 0.5rem;
  padding: 0.6rem 0.75rem;
  border-left: 3px solid var(--warning, #d98e04);
  background: var(--surface-2, rgba(217, 142, 4, 0.08));
  font-size: 0.85rem;
}
.preset-drift ul { margin: 0.35rem 0 0.5rem 1rem; }
```

- [ ] **Step 3: Verify it compiles**

Run: `cd frontend && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/ParameterSetPicker.tsx frontend/src/styles.css
git commit -m "feat(ui): add a parameter-set picker with a persistent drift notice"
```

---

## Task 8: Mount the picker in the align and assemble dialogs

**Files:**
- Modify: `frontend/src/components/AlignDialog.tsx`, `frontend/src/components/AssembleDialog.tsx`

**Interfaces:**
- Consumes: `ParameterSetPicker` from Task 7; the `from_parameter_set` field from Task 5.
- Produces: launch requests carrying `from_parameter_set` when a set was applied.

- [ ] **Step 1: Mount the picker in `AlignDialog`**

`AlignDialog` computes a merged `params` object around line 108 and has an `onChange(key, value)` seam already used by `AlignerParamFields`. Add state near the other `useState` calls (line ~72):

```tsx
  const [appliedSet, setAppliedSet] = useState<
    { setId: string; name: string; revision: number } | null
  >(null);
  const [appliedValues, setAppliedValues] = useState<Record<string, unknown> | null>(null);
```

Render the picker directly above `<AlignerParamFields …>`, passing the aligner as the tool:

```tsx
  {params?.aligner && (
    <ParameterSetPicker
      tool={params.aligner}
      family="aligner"
      currentParams={params}
      onApply={(values) => {
        Object.entries(values).forEach(([k, v]) => setOverrides((o) => ({ ...o, [k]: v })));
        setAppliedValues(values);
      }}
      onAppliedSetChange={(s) => {
        setAppliedSet(s);
        if (!s) setAppliedValues(null);
      }}
    />
  )}
```

- [ ] **Step 2: Thread provenance into the launch request**

`edited_after_apply` is a shallow compare of the values the resolve call returned against what the form holds now. Add near the submit handler (line ~294):

```tsx
  const editedAfterApply =
    appliedValues !== null &&
    Object.entries(appliedValues).some(
      ([k, v]) => (params as Record<string, unknown>)[k] !== v,
    );

  const fromParameterSet = appliedSet
    ? {
        set_id: appliedSet.setId,
        name: appliedSet.name,
        revision: appliedSet.revision,
        edited_after_apply: editedAfterApply,
      }
    : undefined;
```

and include `from_parameter_set: fromParameterSet` in both launch bodies (the `params: { ...params, chunked }` calls at lines ~299 and ~324).

- [ ] **Step 3: Repeat for `AssembleDialog`**

Same three edits, with `family="assembler"` and the assembler name as `tool`. Repeated rather than abstracted: the two dialogs hold their form state differently, and a shared wrapper would have to know both shapes.

- [ ] **Step 4: Verify it compiles**

Run: `cd frontend && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/AlignDialog.tsx frontend/src/components/AssembleDialog.tsx
git commit -m "feat(ui): offer saved parameter sets in the align and assemble dialogs"
```

---

## Task 9: Show the applied set on the run detail view

**Files:**
- Modify: the run detail component (find it with `grep -rln "PipelineRun\|run.params" frontend/src/components/`)
- Modify: `frontend/src/api/types.ts`

**Interfaces:**
- Consumes: `PipelineRun.from_parameter_set` from Task 5.

- [ ] **Step 1: Add the type**

In `frontend/src/api/types.ts`, on the existing run interface:

```typescript
  from_parameter_set?: {
    set_id: string;
    name: string;
    revision: number;
    edited_after_apply: boolean;
  } | null;
```

- [ ] **Step 2: Render the line**

In the run detail view, beside where `params` or `tool` is already shown:

```tsx
{run.from_parameter_set && (
  <p className="run-preset-provenance">
    Preset: {run.from_parameter_set.name} (rev {run.from_parameter_set.revision}
    {run.from_parameter_set.edited_after_apply ? ", edited" : ""})
  </p>
)}
```

Read-only text. If the set was since deleted the line still renders, because the snapshot is stored on the run.

- [ ] **Step 3: Verify it compiles**

Run: `cd frontend && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/components/
git commit -m "feat(ui): show which parameter set configured a run"
```

---

## Task 10: Full-suite verification and manual check

**Files:** none — verification only.

- [ ] **Step 1: Run the whole backend suite**

Run: `./backend/run-worktree-tests.sh tests/ -q`
Expected: no failures. Read the count, not the exit code.

- [ ] **Step 2: Lint as CI does**

Run: `cd backend && ruff check app tests && ruff format --check app tests`
Expected: clean. CI runs `ruff check` with rules a local pytest run never invokes — `I001` (import order) is the one that has bitten this repo before.

- [ ] **Step 3: Bring up the worktree stack**

Run: `./ops/worktree-up.sh`
Expected: UI on 5273, API on 8100. Never plain `docker compose` from a worktree.

- [ ] **Step 4: Manual verification**

There is no headless component-testing setup in this repo, so this is the real verification step for the UI. At localhost:5273:

1. Open an align dialog, set some knobs, "Save current as…" → the preset appears in the picker.
2. Re-open the dialog, apply it → the fields fill.
3. Confirm the saved set holds no reference or input ids (`GET /api/v1/parameter-sets?tool=minimap2` on port 8100).
4. Force drift: `db.parameter_sets.updateOne({}, {$set: {params: {threads: 8, gone: 1}}})`, apply again → the persistent notice names `gone`, and **stays visible** until dismissed.
5. Apply, edit a filled field, launch → the run detail shows `(rev N, edited)`.
6. Rename the set, then re-check that run → it still shows the *old* name, proving the snapshot.

- [ ] **Step 5: Bring the stack down**

Run: `./ops/worktree-up.sh --down`
A stack brought up for testing is yours to bring back down.

- [ ] **Step 6: Close out the issue**

Move [#414](https://github.com/syntheticgio/bioflow/issues/414) from `status:specification document` to `status:implementation plan` when this plan lands, and to `status:ready` when Task 9 is done. File the trim follow-up:

```bash
gh issue create --title "feat(pipelines): give trim_reads a ParamSpec so it can use parameter sets" --label "type:feature,area:pipelines,status:specification document,priority:medium"
```

---

## Notes for the implementer

**Why there is no `sanitize()` call anywhere here.** Issue #414's question 5 asks for one. It is based on a misreading — `params_sanitizer` is a disclosure boundary for what reaches uploaded computation records, not input validation, and freshly typed parameters never pass through it either. Its fourteen-key allowlist rejects any value containing `/`, `\`, or `~`, so applying it on the parameter-set path would strip every reference path and most real knobs. Task 3 has a test that fails if someone adds it back. If you think that reasoning is wrong, raise it rather than quietly "fixing" it — the test is there to make the decision visible, not to be deleted.

**Why v1 stops at aligners and assemblers.** Both have a declarative `ParamSpec`; nothing else does. Adding a hand-written eligible-key dict for the other tools would reproduce the registry shape that cost the STAR `build_index` job its eight index files with the suite green throughout. The fix is to give those tools a `ParamSpec`, which is the follow-up in Task 10 Step 6 — not to work around their not having one.

**Task 8's repetition is deliberate.** `AlignDialog` and `AssembleDialog` hold form state differently, so a shared wrapper would have to know both shapes. Two similar call sites are cheaper here than one abstraction that fits neither.
