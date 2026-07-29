# Manual Paired-End Read Tagging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user mark two reads files as paired-end mates by hand — setting R1/R2 and the symmetric mate pointer — and undo it, with the choice surviving re-ingest.

**Architecture:** Add a `read_number` field to `DataObject`, then two dedicated endpoints (`POST`/`DELETE /objects/{id}/pair`) that write both documents symmetrically. Manual pairings are recorded in the existing `user_touched` list under the key `"mate"`, which filename inference in `_link_mate` then refuses to override — the same three-part pattern already used for `role`. The UI is a new `PairEditor` component in the DetailPanel beside `RoleConverter`, with candidates filtered client-side from the already-cached project object list.

**Tech Stack:** FastAPI + Beanie/Motor (MongoDB) on the backend, React + TanStack Query on the frontend, pytest for backend tests.

**Spec:** `docs/superpowers/specs/2026-07-29-manual-read-pairing-design.md`

---

## Background for the implementer

Read this before starting. It explains the one non-obvious idea the whole feature rests on.

### The `user_touched` pattern

This codebase has a standing promise: **a value the user set explicitly is never overwritten by later automatic inference.** Files get re-ingested (format re-detection, header re-parsing) and inference runs again each time, so this needs a durable mechanism.

The naive approach — "only infer when the field is empty" — has a hole. If the user *clears* a value, the field is empty again, and re-ingest happily re-asserts what the user just removed. A cleared value and a never-set value look identical.

`DataObject.user_touched: list[str]` (`backend/app/models/object.py:158`) closes that hole by recording the *field names* the user has set or cleared. Inference checks the list, not just the value.

For `role`, three pieces cooperate:

1. `apply_role_update` (`backend/app/services/object_service.py:454`) appends `"role"` to `user_touched`.
2. `should_assign_reference_role` (`backend/app/queue/results.py:38`) returns `False` when `"role" in user_touched`.
3. The conditional write at `backend/app/queue/results.py:165` *also* puts `{"user_touched": {"$ne": "role"}}` in the query filter. This matters because the decision is made from an in-memory snapshot read seconds earlier (before a network call); putting the check in the query means a change landing in that window cannot be overruled.

This feature reuses all three ideas with the key `"mate"`.

### Conditional writes and races

`_link_mate` (`backend/app/queue/results.py:187`) writes two documents. It does this with `find_one(...).update(...)` where the filter includes the precondition, then checks `modified_count`:

```python
linked = await DataObject.find_one(
    DataObject.id == mate.id,
    DataObject.mate_object_id == None,  # noqa: E711
).update({"$set": {DataObject.mate_object_id: obj.id}})
if not getattr(linked, "modified_count", 0):
    log.info("mate_link_skipped_raced", ...)
    return
```

If the precondition fails, nothing was written and it stops **before** touching the second document — so a lost race leaves no half-formed link. `set_pair` follows this exact shape.

Note the `== None` with `# noqa: E711`: Beanie needs the identity comparison to build a Mongo query, so ruff's "comparison to None" rule is suppressed deliberately. Keep the noqa comments.

### Running things

Tests run **inside the container**, not the host venv (the host hits Mongo replica-set errors):

```bash
docker compose exec api python -m pytest tests/ -q
```

Run `docker compose` from the **main repo root** (`/Users/syntheticgio/Programming/local-bio-pipeliner`), never from a worktree — the bind mounts are relative paths and running from a worktree silently repoints the shared stack. See CLAUDE.md.

The `api` service hot-reloads (`uvicorn --reload`), so backend edits take effect on the next request. The `worker` service does **not** — but nothing in this plan changes worker code paths that run in-process during a job, so no worker restart is needed. `_link_mate` runs in the result applier, which lives in the `api`/worker process; if you test re-ingest end-to-end through the UI rather than through pytest, run `docker compose restart worker` first.

---

## File Structure

**Backend — create:**
- `backend/tests/storage/test_read_pairing.py` — all pairing tests: model field, serialization, validation rules, symmetric writes, clear, and the inference-override cases.

**Backend — modify:**
- `backend/app/models/object.py` — add `read_number` field to `DataObject`.
- `backend/app/api/v1/schemas.py` — add `PairRequest`; add `read_number` to `ObjectOut` and `ObjectOut.of`.
- `backend/app/services/object_service.py` — add `set_pair` and `clear_pair`.
- `backend/app/api/v1/objects.py` — add the two routes.
- `backend/app/queue/results.py` — guard `_link_mate` on `"mate"` in `user_touched`; populate `read_number` from inference.

**Frontend — create:**
- `frontend/src/components/PairEditor.tsx` — the pairing control.

**Frontend — modify:**
- `frontend/src/api/types.ts` — add `read_number` to the `DataObject` interface.
- `frontend/src/api/client.ts` — add `pairObject` and `unpairObject`.
- `frontend/src/components/DetailPanel.tsx` — render `PairEditor`.
- `frontend/src/components/DerivedFiles.tsx` — show an R1/R2 chip on the mate row.

Tasks are ordered so each one leaves the tree working: the model lands before the code that reads it, the service before the routes that call it, and the frontend after the API it talks to.

---

## Task 1: Add `read_number` to the model

**Files:**
- Modify: `backend/app/models/object.py:181`
- Test: `backend/tests/storage/test_read_pairing.py` (create)

- [ ] **Step 1: Write the failing test**

Create `backend/tests/storage/test_read_pairing.py`. The Beanie fixture is copied from `test_object_role.py` — Beanie requires `init_beanie` before *any* `Document` is instantiated, even one never saved, and this module constructs `DataObject` directly.

```python
"""Manual paired-end pairing: the override that filename inference cannot make."""

import pytest
from beanie import PydanticObjectId, init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from app.api.v1.schemas import ObjectOut
from app.config import settings
from app.models import ALL_MODELS
from app.models.object import DataObject


@pytest.fixture(scope="module", autouse=True)
async def _init_beanie_models():
    """Beanie requires init_beanie before any Document is instantiated. Connects
    to the same Mongo the app uses but against a throwaway database."""
    from app.db.index_reconcile import reconcile_indexes

    client = AsyncIOMotorClient(settings.mongo_url, tz_aware=True)
    db = client["biopipe_test"]
    for model in ALL_MODELS:
        model_settings = model.Settings
        coll_name = getattr(model_settings, "name", model.__name__.lower())
        indexes = getattr(model_settings, "indexes", [])
        if indexes:
            await reconcile_indexes(db[coll_name], indexes)
    await init_beanie(database=db, document_models=ALL_MODELS)
    yield
    client.close()


def _obj(**kw) -> DataObject:
    """A DataObject built without touching the database."""
    defaults = dict(project_id=PydanticObjectId(), name="sample.fastq.gz")
    return DataObject(**{**defaults, **kw})


class TestReadNumberField:
    def test_defaults_to_none(self):
        """Unpaired and unknown are the same state: no read number."""
        assert _obj().read_number is None

    def test_accepts_one_and_two(self):
        assert _obj(read_number=1).read_number == 1
        assert _obj(read_number=2).read_number == 2

    def test_round_trips_through_serialization(self):
        dumped = _obj(read_number=2).model_dump(mode="json")
        assert dumped["read_number"] == 2
        assert _obj(**{"read_number": dumped["read_number"]}).read_number == 2


class TestReadNumberSerialization:
    def test_object_out_exposes_read_number(self):
        assert ObjectOut.of(_obj(read_number=1)).read_number == 1

    def test_object_out_read_number_is_none_when_unset(self):
        assert ObjectOut.of(_obj()).read_number is None
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker compose exec api python -m pytest tests/storage/test_read_pairing.py -v
```

Expected: FAIL. `TestReadNumberField::test_accepts_one_and_two` errors because Pydantic rejects the unknown field `read_number`, and the `ObjectOut` tests fail with `AttributeError` / validation errors.

- [ ] **Step 3: Add the model field**

In `backend/app/models/object.py`, directly below the `mate_object_id` declaration (line 181) and inside the same comment block's topic, add:

```python
    # Which half of a paired-end run this file is: 1 or 2. Set and cleared
    # together with mate_object_id -- a read number without a mate describes a
    # pair that does not exist.
    #
    # A plain int rather than an enum: the domain is closed by biology at
    # {1, 2}, and an enum whose members are ONE and TWO reads worse at every
    # use site than the integer does. The request schema does the validating.
    read_number: int | None = None
```

- [ ] **Step 4: Add it to the API schema**

In `backend/app/api/v1/schemas.py`, add the field to `ObjectOut` immediately after `mate_object_id: str | None` (line 118):

```python
    read_number: int | None
```

and in `ObjectOut.of`, immediately after the `mate_object_id=...` line (line 142):

```python
            read_number=o.read_number,
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
docker compose exec api python -m pytest tests/storage/test_read_pairing.py -v
```

Expected: PASS, 5 tests.

- [ ] **Step 6: Run the whole suite to check nothing regressed**

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: all pass. `ObjectOut` gained a required field, so this catches any construction site that builds one positionally or by hand.

- [ ] **Step 7: Commit**

```bash
git add backend/app/models/object.py backend/app/api/v1/schemas.py backend/tests/storage/test_read_pairing.py
git commit -m "feat: add read_number to DataObject"
```

---

## Task 2: The `PairRequest` schema

**Files:**
- Modify: `backend/app/api/v1/schemas.py`
- Test: `backend/tests/storage/test_read_pairing.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/storage/test_read_pairing.py`. Add `PairRequest` to the existing `from app.api.v1.schemas import ...` line at the top so it reads:

```python
from app.api.v1.schemas import ObjectOut, PairRequest
```

Then append this class:

```python
class TestPairRequest:
    """read_number is validated at the edge, so the service can trust it."""

    def test_accepts_one_and_two(self):
        mate = PydanticObjectId()
        assert PairRequest(mate_object_id=mate, read_number=1).read_number == 1
        assert PairRequest(mate_object_id=mate, read_number=2).read_number == 2

    def test_rejects_zero(self):
        with pytest.raises(ValueError):
            PairRequest(mate_object_id=PydanticObjectId(), read_number=0)

    def test_rejects_three(self):
        with pytest.raises(ValueError):
            PairRequest(mate_object_id=PydanticObjectId(), read_number=3)

    def test_requires_a_mate(self):
        with pytest.raises(ValueError):
            PairRequest(read_number=1)
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker compose exec api python -m pytest tests/storage/test_read_pairing.py::TestPairRequest -v
```

Expected: FAIL at import — `ImportError: cannot import name 'PairRequest'`.

- [ ] **Step 3: Add the schema**

In `backend/app/api/v1/schemas.py`, directly after the `ObjectUpdate` class (which ends at line 75 with `role: ObjectRole | None = None`), add:

```python
class PairRequest(BaseModel):
    """Pairing two reads files by hand.

    Separate from ObjectUpdate because pairing writes *two* documents and its
    central validation question -- is this candidate already attached to a
    third file -- cannot be answered from the single object a PATCH fetches.
    """

    mate_object_id: PydanticObjectId
    # Which half the *subject* is. The mate is always given the other one, so
    # two R1s cannot be produced by a well-formed request.
    read_number: int = Field(ge=1, le=2)
```

`BaseModel` and `Field` are already imported from `pydantic` on line 9. `PydanticObjectId` is **not** imported in this file — add it near the other imports at the top:

```python
from beanie import PydanticObjectId
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
docker compose exec api python -m pytest tests/storage/test_read_pairing.py::TestPairRequest -v
```

Expected: PASS, 4 tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/v1/schemas.py backend/tests/storage/test_read_pairing.py
git commit -m "feat: add PairRequest schema"
```

---

## Task 3: `set_pair` validation rules

This task builds only the validation. The writes come in Task 4, so the tests here assert that bad input raises and stop there.

**Files:**
- Modify: `backend/app/services/object_service.py`
- Test: `backend/tests/storage/test_read_pairing.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/storage/test_read_pairing.py`. First extend the imports at the top of the file:

```python
from app.errors import NotFoundError, ValidationError
from app.models import ObjectRole, SidecarRole
from app.services import object_service
```

These tests hit the database, so they need saved objects. Add this helper next to `_obj`:

```python
async def _saved(project_id: PydanticObjectId, name: str, **kw) -> DataObject:
    """A DataObject persisted to the throwaway test database."""
    obj = DataObject(project_id=project_id, name=name, **kw)
    await obj.insert()
    return obj
```

Then append:

```python
class TestSetPairValidation:
    """Every rejection the endpoint can produce, in the order they are checked.

    Strict by design: correcting a wrong pairing is unpair-then-pair, so no
    request can ever displace a third file's mate as a side effect.
    """

    async def test_rejects_pairing_with_itself(self):
        pid = PydanticObjectId()
        a = await _saved(pid, "a.fastq.gz")
        with pytest.raises(ValidationError, match="itself"):
            await object_service.set_pair(a.id, a.id, 1)

    async def test_rejects_a_missing_mate(self):
        pid = PydanticObjectId()
        a = await _saved(pid, "a.fastq.gz")
        with pytest.raises(NotFoundError):
            await object_service.set_pair(a.id, PydanticObjectId(), 1)

    async def test_rejects_a_mate_in_another_project(self):
        a = await _saved(PydanticObjectId(), "a.fastq.gz")
        b = await _saved(PydanticObjectId(), "b.fastq.gz")
        with pytest.raises(ValidationError, match="same project"):
            await object_service.set_pair(a.id, b.id, 1)

    async def test_rejects_when_the_subject_is_already_paired(self):
        pid = PydanticObjectId()
        other = await _saved(pid, "other.fastq.gz")
        a = await _saved(pid, "a.fastq.gz", mate_object_id=other.id)
        b = await _saved(pid, "b.fastq.gz")
        with pytest.raises(ValidationError, match="already paired"):
            await object_service.set_pair(a.id, b.id, 1)

    async def test_rejects_when_the_mate_is_already_paired(self):
        """Never displace a third file's pairing as a side effect."""
        pid = PydanticObjectId()
        third = await _saved(pid, "third.fastq.gz")
        a = await _saved(pid, "a.fastq.gz")
        b = await _saved(pid, "b.fastq.gz", mate_object_id=third.id)
        with pytest.raises(ValidationError, match="already paired"):
            await object_service.set_pair(a.id, b.id, 1)

    async def test_rejects_a_reference(self):
        pid = PydanticObjectId()
        a = await _saved(pid, "a.fastq.gz")
        ref = await _saved(pid, "genome.fa", role=ObjectRole.REFERENCE)
        with pytest.raises(ValidationError, match="reads"):
            await object_service.set_pair(a.id, ref.id, 1)

    async def test_rejects_a_sidecar(self):
        pid = PydanticObjectId()
        a = await _saved(pid, "a.fastq.gz")
        parent = await _saved(pid, "genome.fa")
        bai = await _saved(
            pid, "genome.fa.fai", sidecar_of=parent.id, sidecar_role=SidecarRole.FAI
        )
        with pytest.raises(ValidationError, match="reads"):
            await object_service.set_pair(a.id, bai.id, 1)

    async def test_rejects_when_the_subject_is_a_reference(self):
        """Checked on both sides, not just the candidate."""
        pid = PydanticObjectId()
        ref = await _saved(pid, "genome.fa", role=ObjectRole.REFERENCE)
        b = await _saved(pid, "b.fastq.gz")
        with pytest.raises(ValidationError, match="reads"):
            await object_service.set_pair(ref.id, b.id, 1)

    async def test_allows_trimmed_reads(self):
        """Trimmed output pairs like any other reads -- the point of the
        feature is files whose signals are missing, so over-filtering by
        format would recreate the gap it exists to close."""
        pid = PydanticObjectId()
        a = await _saved(pid, "a.trimmed.fastq.gz", role=ObjectRole.TRIMMED_READS)
        b = await _saved(pid, "b.trimmed.fastq.gz", role=ObjectRole.TRIMMED_READS)
        result = await object_service.set_pair(a.id, b.id, 1)
        assert result.mate_object_id == b.id
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker compose exec api python -m pytest tests/storage/test_read_pairing.py::TestSetPairValidation -v
```

Expected: FAIL — `AttributeError: module 'app.services.object_service' has no attribute 'set_pair'`.

- [ ] **Step 3: Write the implementation**

In `backend/app/services/object_service.py`, add after `apply_role_update` (which ends at line 472):

```python
def _is_reads(obj: DataObject) -> bool:
    """Whether a file is something that can have a paired-end mate.

    Deliberately not a format check. The feature exists for files whose
    conventional signals are missing, so restricting to FASTQ would recreate
    the gap it closes -- and trimmed reads pair exactly like raw ones.
    """
    return obj.role is not ObjectRole.REFERENCE and obj.sidecar_of is None


async def set_pair(
    object_id: PydanticObjectId,
    mate_object_id: PydanticObjectId,
    read_number: int,
) -> DataObject:
    """Pair two reads files by hand, symmetrically.

    The mate always receives the opposite read number, so a request cannot
    produce two R1s. Both sides record `"mate"` in user_touched, which is what
    stops filename inference from overriding the choice on a later re-ingest.

    Strict about preconditions: both sides must currently be unpaired. The
    dropdown already filters paired candidates out, so a rejection here means a
    stale tab or a script -- and displacing a third file's pairing silently
    would be worse than an error.
    """
    if object_id == mate_object_id:
        raise ValidationError("A file cannot be paired with itself")

    obj = await get_object(object_id)
    mate = await get_object(mate_object_id)

    if obj.project_id != mate.project_id:
        raise ValidationError("Both files must be in the same project")
    if obj.mate_object_id is not None:
        raise ValidationError(f"{obj.name} is already paired; unpair it first")
    if mate.mate_object_id is not None:
        raise ValidationError(f"{mate.name} is already paired; unpair it first")
    if not _is_reads(obj) or not _is_reads(mate):
        raise ValidationError("Only reads files can be paired")

    return obj
```

The write itself is Task 4; returning `obj` unchanged is enough to make these tests pass and keeps this task to one idea.

Check the imports at the top of `object_service.py`. `ValidationError` and `NotFoundError` are already imported (line 10). Confirm `ObjectRole` is in the `from app.models import (...)` block at line 12 — if it is not, add it.

- [ ] **Step 4: Run the test to verify it passes**

```bash
docker compose exec api python -m pytest tests/storage/test_read_pairing.py::TestSetPairValidation -v
```

Expected: PASS, 9 tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/object_service.py backend/tests/storage/test_read_pairing.py
git commit -m "feat: validation rules for manual read pairing"
```

---

## Task 4: `set_pair` symmetric writes

**Files:**
- Modify: `backend/app/services/object_service.py`
- Test: `backend/tests/storage/test_read_pairing.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/storage/test_read_pairing.py`:

```python
class TestSetPairWrites:
    async def test_sets_both_pointers(self):
        pid = PydanticObjectId()
        a = await _saved(pid, "fwd.fastq.gz")
        b = await _saved(pid, "rev.fastq.gz")

        await object_service.set_pair(a.id, b.id, 1)

        a_after = await DataObject.get(a.id)
        b_after = await DataObject.get(b.id)
        assert a_after.mate_object_id == b.id
        assert b_after.mate_object_id == a.id

    async def test_gives_the_mate_the_opposite_read_number(self):
        """The collision rule is structural: two R1s are unreachable."""
        pid = PydanticObjectId()
        a = await _saved(pid, "fwd.fastq.gz")
        b = await _saved(pid, "rev.fastq.gz")

        await object_service.set_pair(a.id, b.id, 1)

        assert (await DataObject.get(a.id)).read_number == 1
        assert (await DataObject.get(b.id)).read_number == 2

    async def test_read_number_two_flips_the_other_way(self):
        pid = PydanticObjectId()
        a = await _saved(pid, "rev.fastq.gz")
        b = await _saved(pid, "fwd.fastq.gz")

        await object_service.set_pair(a.id, b.id, 2)

        assert (await DataObject.get(a.id)).read_number == 2
        assert (await DataObject.get(b.id)).read_number == 1

    async def test_marks_both_sides_user_touched(self):
        """The durable record that stops re-ingest from overriding this."""
        pid = PydanticObjectId()
        a = await _saved(pid, "fwd.fastq.gz")
        b = await _saved(pid, "rev.fastq.gz")

        await object_service.set_pair(a.id, b.id, 1)

        assert "mate" in (await DataObject.get(a.id)).user_touched
        assert "mate" in (await DataObject.get(b.id)).user_touched

    async def test_does_not_duplicate_the_touch(self):
        """$addToSet, so pairing a file that was paired before stays clean."""
        pid = PydanticObjectId()
        a = await _saved(pid, "fwd.fastq.gz", user_touched=["mate"])
        b = await _saved(pid, "rev.fastq.gz")

        await object_service.set_pair(a.id, b.id, 1)

        assert (await DataObject.get(a.id)).user_touched.count("mate") == 1

    async def test_preserves_an_existing_role_touch(self):
        """user_touched is shared across fields; pairing must not clobber it."""
        pid = PydanticObjectId()
        a = await _saved(pid, "fwd.fastq.gz", user_touched=["role"])
        b = await _saved(pid, "rev.fastq.gz")

        await object_service.set_pair(a.id, b.id, 1)

        touched = (await DataObject.get(a.id)).user_touched
        assert "role" in touched
        assert "mate" in touched

    async def test_returns_the_updated_subject(self):
        pid = PydanticObjectId()
        a = await _saved(pid, "fwd.fastq.gz")
        b = await _saved(pid, "rev.fastq.gz")

        result = await object_service.set_pair(a.id, b.id, 1)

        assert result.mate_object_id == b.id
        assert result.read_number == 1

    async def test_a_lost_race_leaves_no_half_link(self):
        """If the mate gets paired between validation and the write, the
        subject must not be left pointing at it."""
        pid = PydanticObjectId()
        third = await _saved(pid, "third.fastq.gz")
        a = await _saved(pid, "fwd.fastq.gz")
        b = await _saved(pid, "rev.fastq.gz")

        # Validation reads both as unpaired, then the mate is taken.
        obj = await object_service.get_object(a.id)
        await b.set({DataObject.mate_object_id: third.id})

        with pytest.raises(ValidationError):
            await object_service.set_pair(obj.id, b.id, 1)

        assert (await DataObject.get(a.id)).mate_object_id is None
        assert (await DataObject.get(a.id)).read_number is None
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker compose exec api python -m pytest tests/storage/test_read_pairing.py::TestSetPairWrites -v
```

Expected: FAIL. `test_sets_both_pointers` fails with `assert None == ObjectId(...)` — validation passes but nothing is written yet.

- [ ] **Step 3: Write the implementation**

In `backend/app/services/object_service.py`, replace the `return obj` at the end of `set_pair` with the two conditional writes. The function's tail becomes:

```python
    if not _is_reads(obj) or not _is_reads(mate):
        raise ValidationError("Only reads files can be paired")

    # Conditional on the mate still being unpaired, and checked before the
    # subject is touched -- so losing this race leaves nothing half-written.
    # Same shape as _link_mate's double write.
    linked = await DataObject.find_one(
        DataObject.id == mate.id,
        DataObject.mate_object_id == None,  # noqa: E711
    ).update(
        {
            "$set": {
                DataObject.mate_object_id: obj.id,
                DataObject.read_number: 3 - read_number,
                DataObject.updated_at: datetime.now(UTC),
            },
            "$addToSet": {"user_touched": "mate"},
        }
    )
    if not getattr(linked, "modified_count", 0):
        raise ValidationError(f"{mate.name} was paired by something else; try again")

    await DataObject.find_one(
        DataObject.id == obj.id,
        DataObject.mate_object_id == None,  # noqa: E711
    ).update(
        {
            "$set": {
                DataObject.mate_object_id: mate.id,
                DataObject.read_number: read_number,
                DataObject.updated_at: datetime.now(UTC),
            },
            "$addToSet": {"user_touched": "mate"},
        }
    )

    log.info(
        "pair_set_manually",
        object_id=str(obj.id),
        mate_id=str(mate.id),
        read_number=read_number,
    )
    return await get_object(object_id)
```

`3 - read_number` maps 1→2 and 2→1. `datetime` and `UTC` are already imported at line 4; `log` is defined near the top of the module.

- [ ] **Step 4: Run the test to verify it passes**

```bash
docker compose exec api python -m pytest tests/storage/test_read_pairing.py -v
```

Expected: PASS, all tests including the earlier classes.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/object_service.py backend/tests/storage/test_read_pairing.py
git commit -m "feat: symmetric writes for manual read pairing"
```

---

## Task 5: `clear_pair`

**Files:**
- Modify: `backend/app/services/object_service.py`
- Test: `backend/tests/storage/test_read_pairing.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/storage/test_read_pairing.py`:

```python
class TestClearPair:
    async def test_clears_both_pointers(self):
        pid = PydanticObjectId()
        a = await _saved(pid, "fwd.fastq.gz")
        b = await _saved(pid, "rev.fastq.gz")
        await object_service.set_pair(a.id, b.id, 1)

        await object_service.clear_pair(a.id)

        assert (await DataObject.get(a.id)).mate_object_id is None
        assert (await DataObject.get(b.id)).mate_object_id is None

    async def test_clears_both_read_numbers(self):
        """A read number outliving its pair would collide against a value the
        user believed they had cleared."""
        pid = PydanticObjectId()
        a = await _saved(pid, "fwd.fastq.gz")
        b = await _saved(pid, "rev.fastq.gz")
        await object_service.set_pair(a.id, b.id, 1)

        await object_service.clear_pair(a.id)

        assert (await DataObject.get(a.id)).read_number is None
        assert (await DataObject.get(b.id)).read_number is None

    async def test_keeps_user_touched_on_both_sides(self):
        """The cleared state is itself the user's decision -- this entry is
        what stops re-ingest from undoing it."""
        pid = PydanticObjectId()
        a = await _saved(pid, "fwd.fastq.gz")
        b = await _saved(pid, "rev.fastq.gz")
        await object_service.set_pair(a.id, b.id, 1)

        await object_service.clear_pair(a.id)

        assert "mate" in (await DataObject.get(a.id)).user_touched
        assert "mate" in (await DataObject.get(b.id)).user_touched

    async def test_clearing_from_the_other_side_works_too(self):
        """Pairing is symmetric, so unpair must be reachable from either file."""
        pid = PydanticObjectId()
        a = await _saved(pid, "fwd.fastq.gz")
        b = await _saved(pid, "rev.fastq.gz")
        await object_service.set_pair(a.id, b.id, 1)

        await object_service.clear_pair(b.id)

        assert (await DataObject.get(a.id)).mate_object_id is None
        assert (await DataObject.get(b.id)).mate_object_id is None

    async def test_is_a_no_op_on_an_unpaired_object(self):
        """Idempotent, so a double click is harmless."""
        pid = PydanticObjectId()
        a = await _saved(pid, "lonely.fastq.gz")

        result = await object_service.clear_pair(a.id)

        assert result.mate_object_id is None
        assert result.user_touched == []

    async def test_a_dangling_mate_pointer_still_clears(self):
        """The mate row being gone must not block unpairing the survivor."""
        pid = PydanticObjectId()
        a = await _saved(pid, "fwd.fastq.gz", mate_object_id=PydanticObjectId(), read_number=1)

        result = await object_service.clear_pair(a.id)

        assert result.mate_object_id is None
        assert result.read_number is None

    async def test_can_re_pair_after_clearing(self):
        """The correction path: unpair, then pair with the right file."""
        pid = PydanticObjectId()
        a = await _saved(pid, "fwd.fastq.gz")
        wrong = await _saved(pid, "wrong.fastq.gz")
        right = await _saved(pid, "right.fastq.gz")
        await object_service.set_pair(a.id, wrong.id, 1)

        await object_service.clear_pair(a.id)
        await object_service.set_pair(a.id, right.id, 1)

        assert (await DataObject.get(a.id)).mate_object_id == right.id
        assert (await DataObject.get(right.id)).read_number == 2
        assert (await DataObject.get(wrong.id)).mate_object_id is None
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker compose exec api python -m pytest tests/storage/test_read_pairing.py::TestClearPair -v
```

Expected: FAIL — `AttributeError: module 'app.services.object_service' has no attribute 'clear_pair'`.

- [ ] **Step 3: Write the implementation**

In `backend/app/services/object_service.py`, add directly after `set_pair`:

```python
async def clear_pair(object_id: PydanticObjectId) -> DataObject:
    """Undo a pairing, from either side.

    Clears the pointer *and* the read number on both files, but leaves "mate"
    in user_touched: the cleared state is itself the user's decision, and that
    entry is what stops filename inference from re-asserting the pairing on the
    next re-ingest.

    A no-op on an unpaired object, so the button is idempotent.
    """
    obj = await get_object(object_id)

    cleared = {
        "$set": {
            DataObject.mate_object_id: None,
            DataObject.read_number: None,
            DataObject.updated_at: datetime.now(UTC),
        },
        "$addToSet": {"user_touched": "mate"},
    }

    if obj.mate_object_id is None:
        return obj

    # The mate is cleared by id rather than by fetching it first: the row may
    # be gone (deleted out from under a stale tab), and that must not block
    # unpairing the file the user is actually looking at.
    await DataObject.find_one(DataObject.id == obj.mate_object_id).update(cleared)
    await DataObject.find_one(DataObject.id == obj.id).update(cleared)

    log.info("pair_cleared_manually", object_id=str(obj.id), mate_id=str(obj.mate_object_id))
    return await get_object(object_id)
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
docker compose exec api python -m pytest tests/storage/test_read_pairing.py -v
```

Expected: PASS, all tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/object_service.py backend/tests/storage/test_read_pairing.py
git commit -m "feat: clear_pair to undo a manual pairing"
```

---

## Task 6: The API routes

**Files:**
- Modify: `backend/app/api/v1/objects.py`

- [ ] **Step 1: Add the routes**

There is no route-level test suite for objects in this repo (the service tests above cover the logic), so this task is implementation plus a manual curl check.

In `backend/app/api/v1/objects.py`, extend the schema import on line 8:

```python
from app.api.v1.schemas import BlobOut, ObjectDetail, ObjectOut, ObjectUpdate, PairRequest
```

Then add these two routes after `update_object` (which ends at line 28), before `reingest_object`:

```python
@router.post("/{object_id}/pair", response_model=ObjectOut)
async def pair_object(object_id: PydanticObjectId, body: PairRequest) -> ObjectOut:
    """Mark two reads files as paired-end mates.

    Its own endpoint rather than a field on PATCH because it writes both
    documents and validates across them -- a merge endpoint could leave one
    side pointing at a file that does not point back.
    """
    obj = await object_service.set_pair(object_id, body.mate_object_id, body.read_number)
    return ObjectOut.of(obj)


@router.delete("/{object_id}/pair", response_model=ObjectOut)
async def unpair_object(object_id: PydanticObjectId) -> ObjectOut:
    """Undo a pairing from either side. Idempotent."""
    obj = await object_service.clear_pair(object_id)
    return ObjectOut.of(obj)
```

- [ ] **Step 2: Verify the routes are registered**

`api` hot-reloads, so no rebuild is needed. The routers nest as `/api/v1` (`app/api/v1/__init__.py:19`) + `/objects` (`objects.py:13`), but FastAPI serves the schema from the app root:

```bash
curl -s localhost:8000/openapi.json | grep -o '/api/v1/objects/{object_id}/pair'
```

Expected: the path prints (once per matching key). If nothing prints, check that the app reloaded cleanly:

```bash
docker compose logs --tail 30 api
```

- [ ] **Step 3: Run the whole suite**

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add backend/app/api/v1/objects.py
git commit -m "feat: pair and unpair endpoints"
```

---

## Task 7: Stop inference from overriding a manual pairing

This is the task that makes the feature durable, and it also fixes an existing bug: `_link_mate`'s docstring already promises "a link the user set is never overwritten -- same principle as role," but its only guard is `if obj.mate_object_id is not None: return`, which says nothing about a pairing the user *cleared*.

**Files:**
- Modify: `backend/app/queue/results.py:187-249`
- Test: `backend/tests/storage/test_read_pairing.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/storage/test_read_pairing.py`. Add the import:

```python
from app.queue.results import _link_mate
```

Then append:

```python
class TestInferenceRespectsManualPairing:
    """Filename inference must never overrule a person -- the same promise
    user_touched already keeps for role."""

    async def test_a_manual_pairing_survives_reingest(self):
        """The motivating case: names say one thing, the user said another."""
        pid = PydanticObjectId()
        # Names that inference *would* pair with each other.
        r1 = await _saved(pid, "s_R1.fastq.gz")
        r2 = await _saved(pid, "s_R2.fastq.gz")
        # But the user pairs R1 with an unconventionally named file instead.
        odd = await _saved(pid, "sampleA_forward.fastq.gz")
        await object_service.set_pair(r1.id, odd.id, 1)

        await _link_mate(await DataObject.get(r1.id))

        assert (await DataObject.get(r1.id)).mate_object_id == odd.id
        assert (await DataObject.get(r2.id)).mate_object_id is None

    async def test_a_cleared_pairing_is_not_re_inferred(self):
        """The hole in the old guard: cleared looks identical to never-set."""
        pid = PydanticObjectId()
        r1 = await _saved(pid, "s_R1.fastq.gz")
        r2 = await _saved(pid, "s_R2.fastq.gz")
        await object_service.set_pair(r1.id, r2.id, 1)
        await object_service.clear_pair(r1.id)

        await _link_mate(await DataObject.get(r1.id))

        assert (await DataObject.get(r1.id)).mate_object_id is None
        assert (await DataObject.get(r2.id)).mate_object_id is None

    async def test_inference_does_not_pair_into_a_cleared_file(self):
        """Reached from the *other* side: r2 was never touched, so it is a
        candidate -- but r1 was, and must not be pulled back in."""
        pid = PydanticObjectId()
        r1 = await _saved(pid, "s_R1.fastq.gz", user_touched=["mate"])
        r2 = await _saved(pid, "s_R2.fastq.gz")

        await _link_mate(await DataObject.get(r2.id))

        assert (await DataObject.get(r2.id)).mate_object_id is None
        assert (await DataObject.get(r1.id)).mate_object_id is None

    async def test_untouched_files_still_pair_automatically(self):
        """The guard must not break ordinary inference."""
        pid = PydanticObjectId()
        r1 = await _saved(pid, "auto_R1.fastq.gz")
        r2 = await _saved(pid, "auto_R2.fastq.gz")

        await _link_mate(await DataObject.get(r2.id))

        assert (await DataObject.get(r1.id)).mate_object_id == r2.id
        assert (await DataObject.get(r2.id)).mate_object_id == r1.id

    async def test_inference_records_read_numbers(self):
        """split_mate already computes R1/R2 and throws it away; keeping it
        gives inferred pairs their badges for free."""
        pid = PydanticObjectId()
        r1 = await _saved(pid, "auto_R1.fastq.gz")
        r2 = await _saved(pid, "auto_R2.fastq.gz")

        await _link_mate(await DataObject.get(r2.id))

        assert (await DataObject.get(r1.id)).read_number == 1
        assert (await DataObject.get(r2.id)).read_number == 2

    async def test_inferred_pairing_is_not_marked_user_touched(self):
        """Only a person's choice earns the override; inference does not."""
        pid = PydanticObjectId()
        r1 = await _saved(pid, "auto_R1.fastq.gz")
        r2 = await _saved(pid, "auto_R2.fastq.gz")

        await _link_mate(await DataObject.get(r2.id))

        assert (await DataObject.get(r1.id)).user_touched == []
        assert (await DataObject.get(r2.id)).user_touched == []
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker compose exec api python -m pytest tests/storage/test_read_pairing.py::TestInferenceRespectsManualPairing -v
```

Expected: FAIL on `test_a_cleared_pairing_is_not_re_inferred` (inference re-pairs them), `test_inference_does_not_pair_into_a_cleared_file`, and both `read_number` tests. `test_a_manual_pairing_survives_reingest` and `test_untouched_files_still_pair_automatically` should already pass — that is fine, they are regression cover.

- [ ] **Step 3: Guard the early return**

In `backend/app/queue/results.py`, in `_link_mate` (line 187), replace:

```python
    if obj.mate_object_id is not None:
        return
```

with:

```python
    # "mate" in user_touched covers the case the pointer cannot: a pairing the
    # user *cleared* is None, indistinguishable from one never set, so without
    # this the next re-ingest silently restores what they just removed. Exactly
    # the hole user_touched was introduced to close for role.
    if obj.mate_object_id is not None or "mate" in obj.user_touched:
        return
```

- [ ] **Step 4: Exclude decided files from the candidate set**

Still in `_link_mate`, replace the candidate query (line 210):

```python
    candidates = await DataObject.find(
        DataObject.project_id == obj.project_id,
        DataObject.id != obj.id,
        DataObject.mate_object_id == None,  # noqa: E711
    ).to_list()
```

with:

```python
    candidates = await DataObject.find(
        DataObject.project_id == obj.project_id,
        DataObject.id != obj.id,
        DataObject.mate_object_id == None,  # noqa: E711
        # Reached from the other side, a file whose pairing was cleared is
        # still an unpaired name match -- so it has to be excluded here too,
        # not just by the early return above.
        {"user_touched": {"$ne": "mate"}},
    ).to_list()
```

- [ ] **Step 5: Record read numbers and re-check on write**

Still in `_link_mate`, the tail currently reads:

```python
    mate = matches[0]

    linked = await DataObject.find_one(
        DataObject.id == mate.id,
        DataObject.mate_object_id == None,  # noqa: E711
    ).update({"$set": {DataObject.mate_object_id: obj.id}})
    if not getattr(linked, "modified_count", 0):
        log.info("mate_link_skipped_raced", object_id=str(obj.id), mate_id=str(mate.id))
        return

    await DataObject.find_one(
        DataObject.id == obj.id,
        DataObject.mate_object_id == None,  # noqa: E711
    ).update({"$set": {DataObject.mate_object_id: mate.id}})

    log.info("mate_linked", object_id=str(obj.id), mate_id=str(mate.id), name=obj.name)
```

Replace it with:

```python
    mate = matches[0]

    # split_mate already computed which half each file is; recording it gives
    # inferred pairs their R1/R2 badges without a second pass over the names.
    obj_read_number = 1 if split[1] == "R1" else 2

    linked = await DataObject.find_one(
        DataObject.id == mate.id,
        DataObject.mate_object_id == None,  # noqa: E711
        # Re-checked in the query, not just above: a manual pairing landing
        # between the decision and this write would otherwise be overruled by
        # a stale snapshot. Same reasoning as the role assignment.
        {"user_touched": {"$ne": "mate"}},
    ).update(
        {
            "$set": {
                DataObject.mate_object_id: obj.id,
                DataObject.read_number: 3 - obj_read_number,
            }
        }
    )
    if not getattr(linked, "modified_count", 0):
        log.info("mate_link_skipped_raced", object_id=str(obj.id), mate_id=str(mate.id))
        return

    await DataObject.find_one(
        DataObject.id == obj.id,
        DataObject.mate_object_id == None,  # noqa: E711
        {"user_touched": {"$ne": "mate"}},
    ).update(
        {
            "$set": {
                DataObject.mate_object_id: mate.id,
                DataObject.read_number: obj_read_number,
            }
        }
    )

    log.info(
        "mate_linked",
        object_id=str(obj.id),
        mate_id=str(mate.id),
        name=obj.name,
        read_number=obj_read_number,
    )
```

`split` is already in scope from the `split = pairing.split_mate(obj.name)` call earlier in the function, and `split[1]` is the `"R1"`/`"R2"` string.

- [ ] **Step 6: Run the test to verify it passes**

```bash
docker compose exec api python -m pytest tests/storage/test_read_pairing.py -v
```

Expected: PASS, all tests.

- [ ] **Step 7: Run the whole suite**

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: all pass. This is the task most likely to disturb existing tests, since it changes auto-pairing behavior — if something fails here, check whether the test was relying on a cleared pairing being re-inferred.

- [ ] **Step 8: Commit**

```bash
git add backend/app/queue/results.py backend/tests/storage/test_read_pairing.py
git commit -m "fix: inference no longer overrides a cleared pairing

_link_mate's docstring promised that a user-set link is never overwritten,
but guarded only against an existing pointer -- so a cleared pairing was
silently re-inferred on the next ingest. Checks user_touched now, and
records read_number while it has the answer in hand."
```

---

## Task 8: Frontend types and API client

**Files:**
- Modify: `frontend/src/api/types.ts:77`
- Modify: `frontend/src/api/client.ts:112`

- [ ] **Step 1: Add the field to the DataObject interface**

In `frontend/src/api/types.ts`, in the `DataObject` interface after `mate_object_id` (line 77):

```typescript
  /** Which half of a paired-end run: 1 or 2. Null when unpaired. */
  read_number: number | null;
```

- [ ] **Step 2: Add the client methods**

In `frontend/src/api/client.ts`, after `updateObject` (line 110):

```typescript
  pairObject: (id: string, body: { mate_object_id: string; read_number: number }) =>
    request<DataObject>(`/objects/${id}/pair`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  unpairObject: (id: string) =>
    request<DataObject>(`/objects/${id}/pair`, { method: "DELETE" }),
```

- [ ] **Step 3: Verify it typechecks**

```bash
docker compose exec web npx tsc --noEmit
```

Expected: no errors. If the `web` container has no `npx`, run `npm run build` in `frontend/` on the host instead.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/api/client.ts
git commit -m "feat: read_number type and pair API client methods"
```

---

## Task 9: The PairEditor component

**Files:**
- Create: `frontend/src/components/PairEditor.tsx`
- Modify: `frontend/src/components/DetailPanel.tsx:845`

- [ ] **Step 1: Create the component**

Create `frontend/src/components/PairEditor.tsx`. It follows `RoleConverter.tsx`'s structure: a `section` wrapper, a `useMutation` invalidating the same three query keys, and `notify` on both outcomes.

```typescript
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { notify } from "../stores/messageStore";
import type { DataObject } from "../api/types";

interface Props {
  obj: DataObject;
}

/** Whether a file can take a paired-end mate.
 *
 * Mirrors the server's `_is_reads`: not a reference, not scaffolding. Not a
 * format check -- the point of manual pairing is files whose conventional
 * signals are missing, and trimmed reads pair like any others.
 */
function isReads(o: DataObject): boolean {
  return o.role !== "reference" && o.sidecar_of === null;
}

/**
 * Marks two reads files as paired-end mates, or undoes it.
 *
 * Exists because filename inference is otherwise the only writer of the
 * pairing: files without an _R1/_R2 token in their names can never be paired,
 * however obvious the pairing is to the person looking at them.
 *
 * Candidates come from the project listing already in cache for the explorer,
 * filtered to the same predicate the server enforces. The server validates
 * again -- this filter is for usability, not for correctness.
 */
export function PairEditor({ obj }: Props) {
  const qc = useQueryClient();
  const [mateId, setMateId] = useState("");
  const [readNumber, setReadNumber] = useState(1);

  const { data: siblings = [] } = useQuery({
    queryKey: ["objects", obj.project_id],
    queryFn: () => api.listObjects(obj.project_id),
  });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["object", obj.id] });
    // Both sides changed, and the mate may be open in another view.
    qc.invalidateQueries({ queryKey: ["objects", obj.project_id] });
    qc.invalidateQueries({ queryKey: ["search"] });
  };

  const pair = useMutation({
    mutationFn: () =>
      api.pairObject(obj.id, { mate_object_id: mateId, read_number: readNumber }),
    onSuccess: () => {
      setMateId("");
      invalidate();
      notify.success("Files paired");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const unpair = useMutation({
    mutationFn: () => api.unpairObject(obj.id),
    onSuccess: () => {
      invalidate();
      notify.success("Pairing removed");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  // A reference or a sidecar has no mate to have; offering the control would
  // only invite a rejection.
  if (!isReads(obj) || obj.status !== "ready") return null;

  const mate = siblings.find((o) => o.id === obj.mate_object_id);

  if (obj.mate_object_id) {
    return (
      <div className="section">
        <div className="section-title">Paired end</div>
        <div style={{ fontSize: 12, marginBottom: 6 }}>
          {obj.read_number ? `R${obj.read_number}` : "Paired"}
          {" · mate: "}
          <span style={{ color: "var(--text-faint)" }}>
            {mate ? mate.name : "(file no longer in this project)"}
          </span>
        </div>
        <button
          type="button"
          className="btn"
          onClick={() => unpair.mutate()}
          disabled={unpair.isPending}
        >
          {unpair.isPending ? "Removing…" : "Remove pairing"}
        </button>
        <div style={{ color: "var(--text-faint)", fontSize: 11, marginTop: 6 }}>
          Clears the pairing on both files. Neither file is deleted or changed.
        </div>
      </div>
    );
  }

  const candidates = siblings.filter(
    (o) => o.id !== obj.id && o.mate_object_id === null && isReads(o),
  );

  return (
    <div className="section">
      <div className="section-title">Paired end</div>

      {candidates.length === 0 ? (
        <div style={{ color: "var(--text-faint)", fontSize: 11 }}>
          No unpaired reads files in this project to pair with.
        </div>
      ) : (
        <>
          <div style={{ display: "flex", gap: 8, marginBottom: 6 }}>
            {/* No className: selects are styled globally in styles.css, the
                same way AlignDialog and SchemaMetadataEditor use them. */}
            <select
              value={mateId}
              onChange={(e) => setMateId(e.target.value)}
              style={{ flex: 1, minWidth: 0 }}
            >
              <option value="">Select mate…</option>
              {candidates.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
            <select
              value={readNumber}
              onChange={(e) => setReadNumber(Number(e.target.value))}
              title="Which half this file is"
            >
              <option value={1}>R1</option>
              <option value={2}>R2</option>
            </select>
          </div>
          <button
            type="button"
            className="btn"
            onClick={() => pair.mutate()}
            disabled={!mateId || pair.isPending}
          >
            {pair.isPending ? "Pairing…" : "Pair"}
          </button>
          <div style={{ color: "var(--text-faint)", fontSize: 11, marginTop: 6 }}>
            The mate is set to R{3 - readNumber} automatically. Use this when the
            filenames have no R1/R2 marker for pairing to be detected from.
          </div>
        </>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Render it in the DetailPanel**

In `frontend/src/components/DetailPanel.tsx`, add the import beside the `RoleConverter` import (line 24):

```typescript
import { PairEditor } from "./PairEditor";
```

and render it directly after `RoleConverter` (line 845):

```typescript
      <RoleConverter obj={obj} metadataDirty={metadataDirty} />
      <PairEditor obj={obj} />
```

- [ ] **Step 3: Verify it typechecks**

```bash
docker compose exec web npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 4: Check it in the browser**

The stack hot-reloads (`vite dev`), so the component should already be live at `localhost:5173`. Select a ready FASTQ file and confirm the **Paired end** section renders and the two selects are styled like the ones in the Align dialog. Full behavioral verification is Task 11.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/PairEditor.tsx frontend/src/components/DetailPanel.tsx
git commit -m "feat: PairEditor control in the detail panel"
```

---

## Task 10: R1/R2 chip on the mate row

**Files:**
- Modify: `frontend/src/components/DerivedFiles.tsx:41-45`

- [ ] **Step 1: Show the read number on the mate row**

`DerivedFiles` already renders a "Paired with" group. Give it the read number, which is the one place a mate is displayed today.

In `frontend/src/components/DerivedFiles.tsx`, replace:

```typescript
      {mate && (
        <Group label="Paired with">
          <FileRow object={mate} onSelect={select} />
        </Group>
      )}
```

with:

```typescript
      {mate && (
        <Group
          label={
            object.read_number
              ? `Paired with (this file is R${object.read_number})`
              : "Paired with"
          }
        >
          <FileRow object={mate} onSelect={select} />
        </Group>
      )}
```

and in `FileRow`, add the mate's own number beside the existing trimmed chip:

```typescript
      <span className="derived-meta">
        {formatBytes(object.size)}
        {object.read_number && <span className="chip">R{object.read_number}</span>}
        {object.role === "trimmed_reads" && <span className="chip">trimmed</span>}
      </span>
```

- [ ] **Step 2: Verify it typechecks**

```bash
docker compose exec web npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/DerivedFiles.tsx
git commit -m "feat: show read number on the paired-with row"
```

---

## Task 11: End-to-end verification

Per CLAUDE.md, manual testing in the browser is the actual verification step for UI-facing work — there is no component-test setup in this repo.

- [ ] **Step 1: Rebuild and restart the stack**

From the **main repo root**, not the worktree:

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner && docker compose up -d --build api web worker
```

- [ ] **Step 2: Confirm the stack is serving the right tree**

```bash
docker inspect biopipe-worker-1 --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}'
```

Expected: no path contains `.claude/worktrees/`. If one does, the stack is on the wrong tree — re-run Step 1 from the main repo root.

Note: to exercise this branch before merging, use a separate project name and unpublished ports (`docker compose -p biopipe-pairing ...`) rather than repointing the shared stack.

- [ ] **Step 3: Walk the motivating path**

At `localhost:5173`:

1. Upload or register two FASTQ files with names carrying **no** R1/R2 token — e.g. `sampleA_forward.fastq.gz` and `sampleA_reverse.fastq.gz`.
2. Select the first. Confirm the **Paired end** section appears and shows no pairing.
3. Choose the second from the dropdown, leave R1 selected, click **Pair**.
4. Confirm the section now reads `R1 · mate: sampleA_reverse.fastq.gz`, and that *Related files* shows a "Paired with (this file is R1)" group.
5. Select the second file. Confirm it shows `R2` and names the first as its mate — the link is symmetric from both sides.

- [ ] **Step 4: Verify the pairing survives re-ingest**

This is the guarantee the feature is built on.

1. With the pair from Step 3 still set, click **Re-ingest** on the first file.
2. Wait for it to return to ready.
3. Confirm the pairing is unchanged.

- [ ] **Step 5: Verify a cleared pairing stays cleared**

The bug fixed in Task 7. Needs files whose names *do* carry the convention, so inference has something to re-assert.

1. Upload `s_R1.fastq.gz` and `s_R2.fastq.gz`. Confirm they auto-pair and show R1/R2 badges.
2. Click **Remove pairing** on either.
3. Confirm both now show the empty pairing control.
4. Re-ingest one of them.
5. Confirm they stay unpaired. Before this change they would silently re-pair.

- [ ] **Step 6: Verify the validation is reachable**

1. With a pair set, select a third unpaired reads file.
2. Confirm neither already-paired file appears in its dropdown.
3. Confirm a reference file (converted via the Role control) shows no **Paired end** section at all.

- [ ] **Step 7: Run the full backend suite one more time**

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: all pass.

- [ ] **Step 8: Commit any fixes**

If Steps 3–6 turned up problems, fix them and commit. If everything passed, there is nothing to commit — do not create an empty commit.

---

## Notes for the reviewer

**What is deliberately not here**, from the spec's *Not doing* section:

- **Explorer badges and the left-spine connector.** These were described in the original request as existing; they do not (`grep -n "mate\|R1\|R2\|pair" frontend/src/components/ProjectExplorer.tsx` returns nothing). Building them is a separate left-panel layout problem. `read_number` is stored and rendered in the DetailPanel, so that work has its data when someone wants it.
- **A read number without a mate.** Filename inference already produces this state naturally; a manual control needs a real request first.
- **Bulk pairing**, **content-based pair detection**, and **cross-project pairing.**

**The one behavior change to existing functionality** is in Task 7: auto-pairing now skips files whose pairing the user cleared. That is the fix `_link_mate`'s docstring already promised, but it is not purely additive — a user who cleared a pairing expecting re-ingest to restore it will see different behavior.
