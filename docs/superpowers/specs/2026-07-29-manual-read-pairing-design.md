# Manual paired-end read tagging

A way to mark a reads file as paired-end by hand: choose its mate from the
project, say whether it is R1 or R2, and undo the whole thing. Manual pairings
survive re-ingest, the way manual roles already do.

## Problem

Pairing is inferred from filenames and nothing else.

`pipelines/pairing.py` derives a pairing key by stripping a mate token
(`_R1`, `.r2`, `_1`, …) from the end of the name, and `_link_mate`
(`queue/results.py:187`) runs after every ingest to find the one candidate whose
name reduces to the same key with the opposite token. That is the only writer of
`mate_object_id`.

So a pair whose filenames carry no recognized token can never be paired at all.
`sampleA_forward.fastq.gz` / `sampleA_reverse.fastq.gz` reduce to nothing;
`split_mate` returns `None`, `_link_mate` returns early, and there is no other
path to the field. The user can see the two files are mates and has no way to
say so.

The docstring in `pairing.py` is candid that the convention is a guess whose
"cost of being wrong is bounded: the launch dialog shows the detected mate and
lets it be changed." That bound holds for a *wrong* pairing at launch time. It
does not help a *missing* pairing, which the launch dialog can only work around
per-run, never record.

### Two things this spec adds that the premise assumed existed

Both were checked against the branch and are absent:

- **`read_number` does not exist.** `DataObject` (`models/object.py:181`) has
  `mate_object_id` and no read-number field. `grep -rn "read_number" backend/app
  frontend/src` returns nothing. Adding it is part of this work.
- **The explorer renders no pairing UI.** `grep -n "mate\|R1\|R2\|pair"` over
  `components/ProjectExplorer.tsx` returns zero matches — there is no left-spine
  connector and no R1/R2 badge. The only place a mate surfaces today is
  `DerivedFiles.tsx:27`, a plain "Paired with" row in the DetailPanel's *Related
  files* section.

This spec therefore introduces `read_number` as a stored field and displays it
in the DetailPanel. Explorer badges and the connector spine are **out of scope**
— see *Not doing*.

## What already exists

### The `role` override pattern, which this follows

`role` solved the same problem — a user's explicit choice must outlive later
inference — and the mechanism is worth restating because pairing reuses it.

`user_touched: list[str]` (`models/object.py:158`) records field names the user
has set *or cleared*. The distinction is the whole point: a cleared `role` is
`None`, indistinguishable from one never set, so without this list a re-ingest
silently restores what the user just removed.

Three pieces cooperate:

1. `apply_role_update` (`services/object_service.py:454`) keys off the
   **presence of the key** in the PATCH body rather than its non-null-ness, and
   appends `"role"` to `user_touched`. The route passes
   `model_dump(exclude_unset=True)` so absent and explicitly-null stay
   distinguishable.
2. `should_assign_reference_role` (`queue/results.py:38`) refuses to infer a
   role when `"role" in user_touched`.
3. The conditional write that follows it (`results.py:165`) re-checks
   `{"user_touched": {"$ne": "role"}}` in the query itself, so a conversion
   landing between the decision and the write cannot be overruled by a stale
   in-memory snapshot.

### The hole in `_link_mate`

`_link_mate`'s docstring already promises the same guarantee:

> A link the user set is never overwritten -- same principle as role.

It does not implement it. The only guard is:

```python
if obj.mate_object_id is not None:
    return
```

which protects a pairing that currently *exists* and says nothing about one the
user **cleared**. Clear a pairing, re-ingest, and filename inference re-asserts
it — exactly the failure `user_touched` was introduced to prevent, still
present here. Closing it is part of this work.

The conditional double-write in `_link_mate` (`results.py:236-247`) is otherwise
sound: it sets the mate's pointer only while that mate is still unpaired, checks
`modified_count`, and bails without touching the second side if it lost a race.
The new code follows that shape.

## Design

### Model

One field on `DataObject`:

```python
# Which half of a paired-end run this file is. Set together with
# mate_object_id and cleared together with it -- a read number without a
# mate is a fact about a pair that does not exist.
read_number: int | None = None
```

`int | None` rather than an enum: the domain is exactly `{1, 2}`, closed by
biology rather than by this application, and an enum whose members are `ONE` and
`TWO` reads worse at every use site than the integer does. Validation lives in
the request schema.

Exposed as `read_number: int | None` on `ObjectOut` (`api/v1/schemas.py`),
passed through in `ObjectOut.of`, and added to the `DataObject` interface in
`frontend/src/api/types.ts`.

No new index. Pairing is always reached through an object already in hand or
through the project listing, never queried by read number.

### Endpoints

Pairing is a **two-document relational write**, which is why it does not go
through `PATCH /objects/{id}`. Setting a pair mutates both files, and the
central validation question — is this candidate already attached to a third
file — cannot be answered from the single `obj` that `update_object` fetches.
Modeling it as two nullable fields on a field-by-field merge endpoint is
precisely what would allow a half-formed link, one side pointing and the other
not.

Two routes in `api/v1/objects.py`:

```
POST   /objects/{object_id}/pair    body: {mate_object_id, read_number}
DELETE /objects/{object_id}/pair
```

Both return the updated subject as `ObjectOut`. Request schema in `schemas.py`:

```python
class PairRequest(BaseModel):
    mate_object_id: PydanticObjectId
    read_number: int = Field(ge=1, le=2)
```

`ObjectUpdate` is left alone.

### Validation

All in `object_service.set_pair`, all raising `ValidationError` (422). Checked
in this order, so the message the user gets names the first real problem:

| Rule | Why |
| --- | --- |
| mate exists | 404 via `get_object` |
| `mate_object_id != object_id` | A file cannot pair with itself |
| same `project_id` | Pairing across projects is not a concept here |
| subject `mate_object_id is None` | Strict: correct by unpairing first |
| mate `mate_object_id is None` | Never displace a third file's pairing |
| both are reads | Not a reference, not a sidecar (see below) |

The **R1/R2 collision rule is structural rather than checked**: the mate's
`read_number` is always written as `3 - read_number`, so two R1s cannot be
produced by any well-formed request. A malformed `read_number` is already a 422
from `Field(ge=1, le=2)`.

"Both are reads" means `role` is not `REFERENCE` and `sidecar_of` is `None`.
Deliberately *not* restricted to `format.kind == FASTQ`: the point of this
feature is files whose conventional signals are missing, and over-filtering
would recreate the gap it exists to close. `TRIMMED_READS` pairs fine.

**Strict rejection over cascading displacement.** The dropdown filters paired
candidates out, so a rejection can only arrive from a stale tab or a script —
and for those, an error is the honest answer. It also keeps unpair on the
load-bearing correction path rather than as a rarely-exercised escape hatch.

### Writes

`set_pair` mirrors `_link_mate`'s conditional-write shape so that two concurrent
pair requests cannot produce a half-formed link:

1. Conditionally set the **mate** side, guarded on
   `mate_object_id == None`. If `modified_count` is 0, someone paired it first —
   raise `ValidationError` and touch nothing else.
2. Set the **subject** side, guarded the same way.
3. Both sides get `read_number` and `"mate"` appended to `user_touched`
   (via `$addToSet`, so a re-pair does not duplicate the entry).

`clear_pair` sets `mate_object_id = None` and `read_number = None` on both
sides, leaving `"mate"` in `user_touched` on both — the cleared state is
*itself* the user's decision, and that entry is what stops re-ingest from
undoing it.

Unpair clears `read_number` too, not just the pointer. A read number outliving
its pair would be carried into the next pair attempt and collide against a value
the user believed they had cleared. It also keeps `"mate"` honest as a guard
over two fields that genuinely move as one unit. The cost — a user wanting "R1,
mate not yet uploaded" — is already produced automatically by filename
inference, and needs no manual control until someone asks for one.

`clear_pair` on an unpaired object is a no-op returning 200, so the button is
idempotent under a double click.

### Closing the `_link_mate` hole

Two changes, matching `should_assign_reference_role` and its conditional write:

```python
if obj.mate_object_id is not None or "mate" in obj.user_touched:
    return
```

and the candidate query excludes objects the user has decided about, so
inference never pairs *into* a file whose pairing was deliberately cleared:

```python
DataObject.find(
    DataObject.project_id == obj.project_id,
    DataObject.id != obj.id,
    DataObject.mate_object_id == None,
    {"user_touched": {"$ne": "mate"}},
)
```

Both conditional writes gain `{"user_touched": {"$ne": "mate"}}` alongside their
existing `mate_object_id == None` guard, so a pairing set between the decision
and the write is not overruled by a stale snapshot.

`_link_mate` also learns to populate `read_number` from what `split_mate`
already returns — it computes `"R1"`/`"R2"` and currently throws it away.
Inferred pairs get badges for free, without `user_touched`.

### Frontend

New `components/PairEditor.tsx`, rendered in the DetailPanel beside
`RoleConverter` (`DetailPanel.tsx:845`), following that component's structure:
`section` wrapper, `section-title`, a `useMutation` invalidating
`["object", id]` / `["objects", project_id]` / `["search"]`, and `notify`
on both outcomes.

Shown when the object is reads (not reference, not a sidecar, `status ===
"ready"`). Two states:

**Unpaired** — a `<select>` of candidates, an R1/R2 radio pair, and a **Pair**
button disabled until a mate is chosen. Candidates come from the already-cached
`["objects", project_id]` query that `DerivedFiles` uses, filtered client-side
to the same predicate the server enforces: not self, `mate_object_id === null`,
not reference, not a sidecar. Empty candidate list renders an explanatory line
instead of an empty dropdown.

**Paired** — the mate's name, which read number each side is, and an **Unpair**
button. No confirm step: it is one click to undo, matching `RoleConverter`'s
reasoning that a cheap reversible change should not ask twice.

The R1/R2 choice defaults to 1. `read_number` is also surfaced as a plain
`R1`/`R2` chip on the existing "Paired with" row in `DerivedFiles.tsx`, which is
where a mate is already displayed.

No new query keys, no new API surface beyond the two client methods
(`api.pairObject`, `api.unpairObject`).

## Testing

Backend, in `backend/tests/`, following `test_object_role.py`'s pattern of
testing the pure decision function directly plus the service against the DB:

- **Validation** — one test per rule in the table: self-pair, cross-project,
  subject already paired, mate already paired, reference, sidecar,
  `read_number` of 0 / 3 / null.
- **Symmetry** — after `set_pair(a, b, 1)`: `a.read_number == 1`,
  `b.read_number == 2`, both pointers set, `"mate"` in both `user_touched`.
- **Clear** — after `clear_pair(a)`: both pointers and both read numbers null,
  `"mate"` still in both `user_touched`. No-op on an unpaired object.
- **Survives re-ingest** — the case that motivates the feature. Pair manually,
  run `_link_mate` over a name whose convention says otherwise, assert the
  manual pairing stands.
- **Cleared pairing is not re-inferred** — the hole above. Two files named
  `s_R1`/`s_R2`, pair them by inference, clear it, re-run `_link_mate`, assert
  they stay unpaired.
- **Inference sets `read_number`** — `_link_mate` on `s_R1`/`s_R2` yields 1 and 2.
- **Race** — `set_pair` losing the conditional write on the mate raises rather
  than leaving a half-link.

Run with `docker compose exec api python -m pytest tests/ -q`.

Frontend verification is manual at localhost:5173, per CLAUDE.md — there is no
component-test setup. The path worth walking: upload two files with
unconventional names, confirm neither is paired, pair them from the panel,
confirm both sides show it, unpair, re-ingest, confirm they stay unpaired.

## Not doing

- **Explorer badges and the left-spine connector.** The premise described these
  as existing; they do not, and building them is a self-contained left-panel
  layout change with its own design questions (how a spine survives sectioning
  and sorting). `read_number` is stored and rendered in the DetailPanel, so that
  work has its data when someone wants it.
- **A read number without a mate.** Filename inference already produces this
  state; a manual control for it needs a real request first.
- **Bulk pairing.** `BulkEditBar` exists, but pairing is inherently a
  two-file operation and "pair these 40 files" has no unambiguous meaning.
- **Content-based pair detection.** Comparing read IDs inside both files is more
  reliable than filenames, as `pairing.py` notes — and is a pipeline job, not a
  UI affordance.
- **Cross-project pairing.** Not a concept in this app.
