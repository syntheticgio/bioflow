# Visually connecting paired-end reads in the file list

## Problem

The left panel's Reads category lists paired-end FASTQ files as independent
rows. Nothing shows that `sample_R1.fastq.gz` and `sample_R2.fastq.gz` are two
halves of one run. The relationship is already known to the backend --
`DataObject.mate_object_id` is a symmetric pointer between mates -- but it is
never rendered.

Two constraints shape the solution. The connection must be unobtrusive, and the
two files must remain separately examinable: each keeps its own click target,
selection state, and delete button. This is not a merge into one row.

## Design

### 1. Backend: a stored read number

`DataObject` gains:

```python
read_number: int | None = None  # 1 or 2
```

placed beside `mate_object_id`, with a comment noting it is inferred from the
filename convention at ingest and overridable on the same terms -- the
convention is only a convention.

Nullable default, so existing documents need no backfill: an absent field reads
as `None`. Pairs already in the database show the spine (which needs only
`mate_object_id`) but no badges until re-linked or manually tagged.

Populated in `_link_mate` in `app/queue/results.py` (~line 190), which already
calls `pairing.split_mate(obj.name)` and therefore has the mate token in hand at
no extra cost. Both sides are set within the same conditional updates that
establish `mate_object_id`, so a pair `_link_mate` creates is never
half-labelled -- the existing race guard (`mate_object_id == None` on both
sides) covers the new field unchanged, as does the existing rule that a
user-set link is never overwritten by later inference.

Two other call sites also set `mate_object_id` directly, from pairing that is
already known rather than inferred: trimmed-pair output (~line 337) and
SRA-downloaded pairs (~line 446). Neither sets `read_number` -- this change
does not extend to them, so pairs created through those paths show a spine
with no badge permanently, not just until re-ingested. That is within the
badge-or-nothing design (a missing badge degrades gracefully) but is worth
naming precisely: "re-ingest and it'll get a badge" is true only for pairs
`_link_mate` handles.

`read_number` derives from `pairing.split_mate`, never from
`facts["paired_hint"]`.

#### Why not `facts["paired_hint"]`

`parsers._infer_pair_hint` already writes an "R1"/"R2" value into `facts`. It is
deliberately not reused, for two reasons.

It disagrees with the pairing. `_infer_pair_hint` substring-matches anywhere in
the name (`"_r1" in name`), while `pairing.split_mate` anchors the token at the
end of the stem. The two return different answers on names carrying more than
one token: for `sample_R1_run_2.fastq`, `_infer_pair_hint` says R1 and
`split_mate` says R2.

Notably, R1 is the better answer there -- anchoring is what misreads that name,
which is the inverse of the case the `pairing` docstring cites as motivation for
anchoring. Accuracy is therefore *not* the argument for `split_mate`.

The argument is consistency. `mate_object_id` is established by `is_mate_of`,
which is built on `split_mate`. A badge derived from a different function than
the pairing itself can contradict the spine -- claiming two files are one run
while labelling both R1 -- and a visible self-contradiction is worse than either
function being wrong alone. The badge must agree with the pointer it annotates,
so it comes from the same source.

Where both functions are wrong about a name, the pair is mislabelled
consistently, and the manual tagging feature is the fix.

It also lives in the wrong place. `facts` is parser output; the model's comments
describe `metadata` as the user-owned, user-editable store. A field a future
tagging UI writes to should not be a parse-time artifact that re-ingest
overwrites.

`paired_hint` is therefore treated as legacy: left in place, not read for
display. Its existing callers are not refactored -- nothing currently reads it
for display, so that is out of scope here.

### 2. API

`ObjectOut` (`app/api/v1/schemas.py`) gains `read_number: int | None`, mapped
straight through in `.of()`. `DataObject` in `frontend/src/api/types.ts` gains
the matching field.

### 3. Frontend: ordering

The spine can only be drawn between adjacent rows, so the Reads category gets an
explicit sort in place of its current incidental order:

- Files group into pair-units keyed on the mate link.
- Within a unit, order by `read_number` -- R1 then R2.
- Units and unpaired files sort together by name, so a pair sits where its name
  puts it rather than being hoisted to the top.

Two fallbacks:

- A pair where neither side has a `read_number` (a manual pairing of files whose
  names carry no token) orders by name within the unit. There is no defined
  R1-first answer, and name order is stable.
- A `mate_object_id` whose target is absent from the list -- deleted, or in
  another project -- renders as unpaired rather than as a dangling half-pair.

### 4. Frontend: spine and badges

Rows in a pair get `paired-first` / `paired-second` classes. CSS draws a 2px
vertical spine with a tick into each row in the row's left gutter via
`::before`, coloured `var(--border)` so it reads as structure rather than
status. The left gutter is currently unused, so nothing is displaced:

```
 ┌─ 📄 sample_R1.fastq.gz     1.2 GB · FASTQ · R1
 └─ 📄 sample_R2.fastq.gz     1.2 GB · FASTQ · R2
```

Badges render `R1` / `R2` in the existing `.row-sub` line from `read_number`,
and render nothing when it is null. That "badge or nothing" shape is what makes
the future tagging UI a data change rather than a component change.

The spine and the badge are not redundant. The spine says *these two files are
one run*; the badge says *which half this is*. When filenames stop carrying the
convention, the badge is the only thing that can answer the second question --
which is the reason the read number is stored rather than derived at render
time.

Rows remain fully independent: separate click targets, selection, and delete
buttons. Deleting one mate leaves the survivor rendering as unpaired, via the
absent-target fallback above.

### 5. Testing

`pytest` (run inside the `api` container per CLAUDE.md) covers the backend
field:

- Mate-linking sets `read_number` on both sides.
- Both naming schemes (`_R1`/`_R2` and `_1`/`_2`).
- Unpaired files get `None`.
- `read_number` agrees with `mate_object_id`: a linked pair never ends up with
  the same read number on both sides. This is the property the badge depends on,
  and it should be asserted directly rather than inferred from the naming cases.

Deliberately not tested: that any particular multi-token filename resolves to a
biologically correct read number. `split_mate`'s answer is the definition here,
and pinning a case like `sample_R1_run_2.fastq` would freeze a known-imperfect
inference into a regression test.

The visual goes to manual verification at localhost:5173, per CLAUDE.md: there
is no headless component-testing setup in this repo and none is expected.

## Out of scope

Manual pairing. A user cannot yet mark a file as paired-end, choose R1 or R2, or
select its mate, so pairs whose filenames lack the convention get neither spine
nor badges. `read_number` is the field that feature will write to; it is tracked
as separate follow-up work.

Refactoring or removing `_infer_pair_hint` and its `facts["paired_hint"]` value.
