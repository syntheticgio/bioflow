# Paired-End Visual Connection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show paired-end FASTQ files as visually connected in the left panel's Reads list -- a left-gutter spine joining the two rows plus `R1`/`R2` badges -- while keeping each file independently selectable and deletable.

**Architecture:** A new nullable `read_number` field on `DataObject`, populated from `pairing.split_mate` inside the existing mate-linking routine that already sets `mate_object_id`. The frontend sorts the Reads category so mates render adjacently, then draws a CSS spine across the pair and a badge per row. No migration: absent field reads as `None`.

**Tech Stack:** Beanie/Motor (MongoDB ODM), FastAPI, Pydantic, React + TypeScript, TanStack Query, plain CSS with custom properties.

**Spec:** `docs/superpowers/specs/2026-07-29-paired-end-visual-design.md`

---

## Background for the implementer

You need three facts about this codebase before starting.

**1. Pairing is filename-derived.** `backend/app/pipelines/pairing.py` has
`split_mate(name) -> (key, mate, scheme) | None`, where `mate` is the string
`"R1"` or `"R2"`. A convenience wrapper `mate_of(name) -> "R1" | "R2" | None`
returns just the mate. Read that module's docstring before you start.

**2. `mate_object_id` is symmetric and race-guarded.** In
`backend/app/queue/results.py`, `_link_mate` (line 187) runs after every ingest.
Whichever mate lands second finds the match and links both sides. The two
updates are conditional on `mate_object_id == None` so two concurrent ingests
cannot produce a half-formed link. You are adding `read_number` to those same
two updates -- do not add a third write.

**3. There is a deliberate non-obvious decision here.** `facts["paired_hint"]`
already holds an "R1"/"R2" value written by `parsers._infer_pair_hint`. Do **not**
use it. It substring-matches anywhere in the name while `split_mate` anchors at
the end of the stem, so the two disagree on names like `sample_R1_run_2.fastq`
(hint says R1, `split_mate` says R2). `read_number` must come from `split_mate`
because `mate_object_id` does -- a badge from a different source could contradict
the spine it annotates. The spec's "Why not `facts['paired_hint']`" section has
the full reasoning. Leave `_infer_pair_hint` and its callers untouched.

**Running tests:** always inside the container, per `CLAUDE.md`:

```bash
docker compose exec api python -m pytest tests/ -q
```

The host `.venv` hits Mongo replica-set errors. Backend tests that touch Mongo
use the throwaway `biopipe_test` database (see
`backend/tests/db/test_index_reconcile.py` for the fixture pattern).

**Docker commands run from the main repo root**, never from a worktree --
`/Users/syntheticgio/Programming/local-bio-pipeliner`. See `CLAUDE.md`.

---

## File Structure

**Backend**
- Modify: `backend/app/models/object.py` -- add `read_number` field beside `mate_object_id` (~line 181)
- Modify: `backend/app/queue/results.py` -- set `read_number` in `_link_mate` (~lines 187-250)
- Modify: `backend/app/api/v1/schemas.py` -- add `read_number` to `ObjectOut` (~line 118) and its `.of()` mapper (~line 142)
- Create: `backend/tests/queue/test_mate_link.py` -- `_link_mate` behavior against Mongo

**Frontend**
- Modify: `frontend/src/api/types.ts` -- add `read_number` to `DataObject` (~line 77)
- Create: `frontend/src/lib/pairing.ts` -- the Reads ordering function, pure and unit-testable by inspection
- Modify: `frontend/src/components/ProjectExplorer.tsx` -- use the ordering, emit pair classes and badges
- Modify: `frontend/src/styles.css` -- spine and badge styles

The ordering logic goes in its own module rather than inline in the component:
it is the one piece here with real edge cases (missing read numbers, mates absent
from the list), and a 400-line component is already large enough.

---

### Task 1: Add the `read_number` field to the model

**Files:**
- Modify: `backend/app/models/object.py:181`

- [ ] **Step 1: Add the field**

In `backend/app/models/object.py`, find the `mate_object_id` field and its
comment block (~line 178-181):

```python
    # The other half of a paired-end run. Symmetric: both mates point at each
    # other. Inferred from the R1/R2 filename convention at ingest and
    # overridable, since the convention is only a convention.
    mate_object_id: PydanticObjectId | None = None
```

Add directly beneath it:

```python
    # Which half of the pair this file is: 1 or 2. Derived from the same
    # `pairing.split_mate` call that establishes `mate_object_id`, so the label
    # can never contradict the link -- see the paired-end design spec. Nullable
    # for single-end files, and for pairs predating this field.
    read_number: int | None = None
```

No index. Nothing queries on read number; it is read only alongside the object
it belongs to.

- [ ] **Step 2: Verify the model still loads**

```bash
docker compose exec api python -c "from app.models.object import DataObject; print(DataObject.model_fields['read_number'])"
```

Expected: field info printed showing `annotation=Union[int, NoneType]` and
`default=None`. No import errors.

- [ ] **Step 3: Commit**

```bash
git add backend/app/models/object.py
git commit -m "feat: add read_number field to DataObject"
```

---

### Task 2: Populate `read_number` in `_link_mate`

**Files:**
- Modify: `backend/app/queue/results.py:187-250`
- Test: `backend/tests/queue/test_mate_link.py` (create)

- [ ] **Step 1: Write the failing test**

Create `backend/tests/queue/test_mate_link.py`:

```python
"""Mate linking sets both the pointer and the read number.

The badge in the file list is driven by `read_number` while the spine that
connects the two rows is driven by `mate_object_id`. Both come from the same
`pairing.split_mate` call for a reason: if they could disagree, the UI would
claim two files are one run while labelling both of them R1. The final test
here is the one that pins that invariant.
"""

import pytest
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from app.config import settings
from app.models.object import DataObject, ObjectStatus
from app.queue.results import _link_mate


@pytest.fixture
async def _db():
    """Throwaway test database, same pattern as tests/db/test_index_reconcile."""
    client = AsyncIOMotorClient(settings.mongo_url, tz_aware=True)
    db = client["biopipe_test"]
    await init_beanie(database=db, document_models=[DataObject])
    await DataObject.delete_all()
    yield db
    await DataObject.delete_all()
    client.close()


async def _obj(name: str, project_id="507f1f77bcf86cd799439011") -> DataObject:
    """A ready FASTQ object carrying just enough to be linkable.

    `owner` is inherited from TimestampedDocument and defaults to "local", so it
    is left alone. Enum members are upper-case: ObjectStatus.READY.
    """
    o = DataObject(
        project_id=project_id,
        name=name,
        size=1024,
        status=ObjectStatus.READY,
    )
    await o.insert()
    return o


class TestLinkMate:
    async def test_links_r_scheme_pair_with_read_numbers(self, _db):
        r1 = await _obj("sample_R1.fastq.gz")
        r2 = await _obj("sample_R2.fastq.gz")

        # The second file to arrive is the one that finds the match.
        await _link_mate(r2)

        r1, r2 = await DataObject.get(r1.id), await DataObject.get(r2.id)
        assert r1.mate_object_id == r2.id
        assert r2.mate_object_id == r1.id
        assert r1.read_number == 1
        assert r2.read_number == 2

    async def test_links_numeric_scheme_pair(self, _db):
        a = await _obj("sample_1.fastq.gz")
        b = await _obj("sample_2.fastq.gz")

        await _link_mate(b)

        a, b = await DataObject.get(a.id), await DataObject.get(b.id)
        assert a.read_number == 1
        assert b.read_number == 2

    async def test_unpaired_file_gets_no_read_number(self, _db):
        solo = await _obj("sample.fastq.gz")

        await _link_mate(solo)

        solo = await DataObject.get(solo.id)
        assert solo.mate_object_id is None
        assert solo.read_number is None

    async def test_read_numbers_never_collide_within_a_pair(self, _db):
        """The invariant the badge depends on.

        Asserted directly rather than inferred from the naming cases above: any
        linked pair must carry one 1 and one 2, whatever the filenames were.
        """
        r1 = await _obj("Sample_R1.fastq")
        r2 = await _obj("sample_R2.fastq")

        await _link_mate(r2)

        r1, r2 = await DataObject.get(r1.id), await DataObject.get(r2.id)
        assert r1.mate_object_id is not None
        assert {r1.read_number, r2.read_number} == {1, 2}
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker compose exec api python -m pytest tests/queue/test_mate_link.py -v
```

Expected: `test_unpaired_file_gets_no_read_number` PASSES (nothing sets the
field yet, so it is already `None`). The other three FAIL on
`assert None == 1` -- the link is established but the read numbers are absent.

- [ ] **Step 3: Set the read numbers in `_link_mate`**

In `backend/app/queue/results.py`, `_link_mate` already computes the split near
the top:

```python
    split = pairing.split_mate(obj.name)
    if split is None or not split[0]:
        return
```

`split[1]` is this object's mate token (`"R1"` or `"R2"`). At the two update
calls near the end of the function, add `read_number` to each `$set`. Replace:

```python
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
```

with:

```python
    # Read numbers come from the same split that matched the pair, so the label
    # and the link cannot disagree. `is_mate_of` already established that the
    # two tokens are opposites, so deriving one from the other is sound.
    this_read = 1 if split[1] == "R1" else 2
    mate_read = 2 if this_read == 1 else 1

    linked = await DataObject.find_one(
        DataObject.id == mate.id,
        DataObject.mate_object_id == None,  # noqa: E711
    ).update(
        {"$set": {DataObject.mate_object_id: obj.id, DataObject.read_number: mate_read}}
    )
    if not getattr(linked, "modified_count", 0):
        log.info("mate_link_skipped_raced", object_id=str(obj.id), mate_id=str(mate.id))
        return

    await DataObject.find_one(
        DataObject.id == obj.id,
        DataObject.mate_object_id == None,  # noqa: E711
    ).update(
        {"$set": {DataObject.mate_object_id: mate.id, DataObject.read_number: this_read}}
    )
```

Both writes stay conditional on `mate_object_id == None`, so the race guard is
unchanged and a pair is never half-labelled.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
docker compose exec api python -m pytest tests/queue/test_mate_link.py -v
```

Expected: all four PASS.

- [ ] **Step 5: Run the pairing and queue suites for regressions**

```bash
docker compose exec api python -m pytest tests/pipelines/test_pairing.py tests/queue/ -q
```

Expected: all pass. If `tests/queue/test_pipeline_handlers.py` fails, check
whether it asserts on an exact `$set` payload from `_link_mate` and update the
expectation.

- [ ] **Step 6: Commit**

```bash
git add backend/app/queue/results.py backend/tests/queue/test_mate_link.py
git commit -m "feat: set read_number when linking mates"
```

---

### Task 3: Expose `read_number` through the API

**Files:**
- Modify: `backend/app/api/v1/schemas.py:118,142`
- Modify: `frontend/src/api/types.ts:77`

- [ ] **Step 1: Add the field to `ObjectOut`**

In `backend/app/api/v1/schemas.py`, the `ObjectOut` class declares
`mate_object_id: str | None` (~line 118). Add beneath it:

```python
    read_number: int | None
```

- [ ] **Step 2: Map it in `.of()`**

In the same file, `ObjectOut.of()` has the line
`mate_object_id=str(o.mate_object_id) if o.mate_object_id else None,` (~line
142). Add beneath it:

```python
            read_number=o.read_number,
```

Passed through as-is: it is already an `int | None` and needs no stringifying.
`ObjectDetail` extends `ObjectOut`, so it inherits the field with no change.

- [ ] **Step 3: Verify the API serves it**

```bash
docker compose exec api python -m pytest tests/api/ -q
```

Expected: all pass. `ObjectOut` has no `model_config` forbidding extra fields,
and every construction goes through `.of()`, so adding a required field is safe
-- but if any test constructs `ObjectOut(...)` directly it will now fail on the
missing argument. Add `read_number=None` to such call sites.

- [ ] **Step 4: Add the field to the frontend type**

In `frontend/src/api/types.ts`, the `DataObject` interface has:

```typescript
  /** The other half of a paired-end run, if known. */
  mate_object_id: string | null;
```

Add directly beneath:

```typescript
  /** Which half of the pair: 1 or 2. Null for single-end files, and for pairs
   *  linked before this field existed. */
  read_number: number | null;
```

- [ ] **Step 5: Verify the frontend typechecks**

```bash
docker compose exec web npx tsc --noEmit
```

Expected: no errors. (If the `web` container has no `npx`, run
`cd frontend && npx tsc --noEmit` on the host instead.)

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/v1/schemas.py frontend/src/api/types.ts
git commit -m "feat: expose read_number through the object API"
```

---

### Task 4: The Reads ordering function

**Files:**
- Create: `frontend/src/lib/pairing.ts`

This is pure logic with real edge cases, so it goes in its own module. There is
no frontend test runner in this repo (see `CLAUDE.md` -- no jsdom, zero
`.test.tsx`), so correctness here rests on the function being small and the
cases being enumerated in comments. Keep it that way: no DOM, no React, no
imports beyond the type.

- [ ] **Step 1: Write the module**

Create `frontend/src/lib/pairing.ts`:

```typescript
import type { DataObject } from "../api/types";

/** Where a file sits in a mate pair, for the connector drawn across the two rows. */
export type PairPosition = "first" | "second" | null;

export interface OrderedFile {
  object: DataObject;
  /** "first" is the top half of a pair, "second" the bottom. Null when unpaired. */
  pair: PairPosition;
}

/**
 * Order a category's files so mates sit adjacent, R1 above R2.
 *
 * The spine can only be drawn between neighbouring rows, so ordering is a
 * prerequisite for the visual rather than a cosmetic choice.
 *
 * Pairs sort by the name of their first member, so a pair stays where its name
 * puts it instead of being hoisted above the unpaired files.
 */
export function orderWithPairs(files: DataObject[]): OrderedFile[] {
  const byId = new Map(files.map((f) => [f.id, f]));
  const consumed = new Set<string>();
  const units: { sortKey: string; entries: OrderedFile[] }[] = [];

  for (const file of files) {
    if (consumed.has(file.id)) continue;

    // A self-referential pointer would emit the same file twice, duplicate
    // React keys and all. `_link_mate` cannot produce one, but the planned
    // manual-tagging feature writes this field directly, so it is guarded here
    // rather than trusted.
    const mateId = file.mate_object_id === file.id ? null : file.mate_object_id;
    const mate = mateId ? byId.get(mateId) : undefined;

    // Unpaired, or half of a pair whose other side is not in this list --
    // deleted, or living in another project. Rendering a spine to nothing
    // would be worse than rendering none, so it reads as a plain file.
    if (!mate) {
      units.push({ sortKey: file.name, entries: [{ object: file, pair: null }] });
      continue;
    }

    consumed.add(file.id);
    consumed.add(mate.id);

    // R1 on top. When neither side carries a read number -- a pair whose names
    // never had the convention -- name order is the stable fallback.
    let top = file;
    let bottom = mate;
    if (file.read_number != null && mate.read_number != null) {
      if (file.read_number > mate.read_number) [top, bottom] = [mate, file];
    } else if (file.name.localeCompare(mate.name) > 0) {
      [top, bottom] = [mate, file];
    }

    units.push({
      sortKey: top.name,
      entries: [
        { object: top, pair: "first" },
        { object: bottom, pair: "second" },
      ],
    });
  }

  units.sort((a, b) => a.sortKey.localeCompare(b.sortKey));
  return units.flatMap((u) => u.entries);
}
```

- [ ] **Step 2: Verify it typechecks**

```bash
docker compose exec web npx tsc --noEmit
```

Expected: no errors. The module is not imported anywhere yet, which is fine.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/lib/pairing.ts
git commit -m "feat: add mate-aware ordering for the reads list"
```

---

### Task 5: Render the spine and badges

**Files:**
- Modify: `frontend/src/components/ProjectExplorer.tsx:360-420`
- Modify: `frontend/src/styles.css`

- [ ] **Step 1: Import the ordering function**

In `frontend/src/components/ProjectExplorer.tsx`, add to the imports at the top:

```typescript
import { orderWithPairs } from "../lib/pairing";
```

- [ ] **Step 2: Apply the ordering and emit classes plus badges**

In `ProjectView`, the category map currently reads:

```tsx
                {isExpanded &&
                  categoryFiles.map((o: DataObject) => (
                    <div
                      key={o.id}
                      className={`row ${sel === `object:${o.id}` ? "selected" : ""}`}
                      onClick={() => select(`object:${o.id}`)}
                    >
```

Replace that opening with:

```tsx
                {isExpanded &&
                  orderWithPairs(categoryFiles).map(({ object: o, pair }) => (
                    <div
                      key={o.id}
                      className={`row ${sel === `object:${o.id}` ? "selected" : ""}${
                        pair ? ` paired paired-${pair}` : ""
                      }`}
                      onClick={() => select(`object:${o.id}`)}
                    >
```

The rest of the row body is unchanged -- same click target, same delete button,
so the two files stay independently selectable and deletable.

Then add the badge to the sub-line. Find:

```tsx
                        <div className="row-sub">
                          <span>{formatBytes(o.size)}</span>
                          {o.format.kind !== "unknown" && (
                            <span>{formatKindLabel(o.format.kind)}</span>
                          )}
                          {o.status !== "ready" && <span>{o.status}</span>}
                        </div>
```

and add one line before the closing `</div>`:

```tsx
                        <div className="row-sub">
                          <span>{formatBytes(o.size)}</span>
                          {o.format.kind !== "unknown" && (
                            <span>{formatKindLabel(o.format.kind)}</span>
                          )}
                          {o.status !== "ready" && <span>{o.status}</span>}
                          {o.read_number != null && (
                            <span className="read-badge">R{o.read_number}</span>
                          )}
                        </div>
```

Renders nothing when `read_number` is null, which is what lets the future
manual-tagging feature light these up as a pure data change.

- [ ] **Step 3: Add the styles**

In `frontend/src/styles.css`, find the `.row-sub` rule (~line 331) and add after
it:

```css
/* ---------- Paired-end reads ---------- */

/* The spine lives in the row's left gutter, which is otherwise unused. Drawn
   in --border so it reads as structure rather than status: it says "one run",
   not "something happened". */
.row.paired {
  position: relative;
  padding-left: 22px;
}

.row.paired::before {
  content: "";
  position: absolute;
  left: 10px;
  width: 2px;
  background: var(--border);
}

/* Each half covers from its own vertical centre to the shared edge, so the two
   pseudo-elements meet as one continuous line across the pair. */
.row.paired-first::before {
  top: 50%;
  bottom: 0;
}

.row.paired-second::before {
  top: 0;
  bottom: 50%;
}

/* The tick into each row, turning the spine into a bracket. */
.row.paired::after {
  content: "";
  position: absolute;
  left: 10px;
  top: 50%;
  width: 6px;
  height: 2px;
  background: var(--border);
}

/* Selection draws its own inset left edge (see .row.selected). Nudging the
   spine clear keeps the two from reading as one thick smear. */
.row.paired.selected::before,
.row.paired.selected::after {
  left: 12px;
}

.read-badge {
  padding: 0 4px;
  border-radius: 3px;
  background: var(--bg-hover);
  color: var(--text-dim);
  font-variant-numeric: tabular-nums;
}
```

- [ ] **Step 4: Verify it typechecks**

```bash
docker compose exec web npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 5: Rebuild and check in the browser**

From the main repo root -- not the worktree:

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner && docker compose up -d --build api web worker
```

`worker` needs the restart because `results.py` is a queue handler and does not
hot-reload; without it, a fresh upload links mates using the old in-memory code
and `read_number` stays null.

Then at localhost:5173, open a project with paired reads and confirm:

- Mates are adjacent, R1 above R2.
- A spine joins the two rows, with a tick into each.
- Each row shows its `R1` / `R2` badge -- **only for pairs uploaded after this
  change.** Pre-existing pairs show the spine but no badges, because
  `read_number` is null and there is no backfill. Upload a fresh pair to see
  badges.
- Clicking either row selects only that row, and the spine stays legible
  against the selection highlight.
- Deleting one mate leaves the other as a plain unpaired row, no dangling spine.
- Single-end files are unchanged: no spine, no badge, no extra indent.
- Toggle the OS light/dark setting and confirm the spine is visible in both --
  `--border` is defined for each theme.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/ProjectExplorer.tsx frontend/src/styles.css
git commit -m "feat: connect paired-end reads visually in the file list"
```

---

## Out of scope

Manual pairing -- marking a file as paired-end, choosing R1 or R2, selecting its
mate -- is tracked as separate follow-up work. `read_number` is the field it will
write to, and the badge already renders "value or nothing", so that feature needs
no component changes here.

Refactoring `parsers._infer_pair_hint` or removing `facts["paired_hint"]`.
Nothing reads it for display; leave it alone.

Backfilling `read_number` for existing pairs. They show the spine without badges
until re-ingested or manually tagged. Verify this reads acceptably in Step 5
above rather than assuming it does.
