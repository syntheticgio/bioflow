# Saving and reusing pipeline parameter sets

**Issue:** [#414](https://github.com/syntheticgio/bioflow/issues/414)
**Date:** 2026-08-18
**Status:** design

## The problem

Pipeline runs are configured per-invocation. A user processing thirty samples
through the same trim-and-align settings re-enters those settings thirty
times, and nothing records that the thirty runs were meant to be identical.

The tedium is the obvious cost. The worse one is silent inconsistency: a
parameter typed differently on sample 17 produces a result that is not
comparable with the other twenty-nine, and nothing surfaces the discrepancy.
In an analysis context that is a correctness problem wearing a convenience
problem's clothes.

## What this is called

**`ParameterSet`, not `Preset`.** The word "preset" is already taken twice in
the subsystem this feature lives in:

- `align_runner.Preset` -- minimap2's `-x` values (`map-ont`, `sr`, ...).
- `AlignerSpec.presets` -- built-in, read-only tuning profiles returned by
  `aligner_registry.schema_for`, which the dialog already renders as a
  selector.

Both are tool-authored and immutable. This feature is user-authored and
per-profile. A third meaning of "preset" in the same modules would be
actively misleading, so the code says `ParameterSet` throughout.

The user-facing label stays "preset" where that reads naturally in the UI --
it is the word the domain uses, and users never see the type name.

## Scope of v1

Parameter sets are offered **only for tools that have a declarative
`ParamSpec`**: aligners (`aligner_registry`) and assemblers
(`assembler_registry`). Every other tool -- trim, QC, variant, quantify --
hardcodes its fields in its dialog and gets no preset UI until it grows a
spec.

This is a deliberate constraint, not an oversight. The entire design derives
its preset-eligible key list and its drift detection from `spec.fields`. A
tool without a spec would need a hand-maintained allowlist of eligible keys,
and a hand-maintained dict keyed by tool -- where a missing entry silently
means "no keys eligible" -- is precisely the silent-skip shape CLAUDE.md's
"Hand-maintained registries keyed by an enum" section warns about. That
section's worked example is the STAR `_SIDECAR_ROLES` incident: a
`build_index` job reported success while storing none of its eight index
files, with the full test suite green throughout. Introducing that shape in
the same change that establishes the feature is not a trade worth making.

**Follow-up:** giving `trim_reads` a `ParamSpec` lights up presets for it with
no further work here. Trim is arguably the most repetitive tool in batch work,
so this is the first follow-up worth filing.

## Decisions

Six questions were settled before design. Five are the issue's own open
questions; the sixth was found during exploration.

### 1. Version drift: apply what matches, flag the rest

When a saved parameter no longer matches the tool's current parameters, fill
every recognized field and show a visible notice naming exactly which saved
keys were not applied and why.

Rejected alternatives:

- **Refuse to apply.** One removed parameter makes an otherwise-good set
  useless, and the user's only recovery is to rebuild it by hand.
- **Drop unknown keys quietly.** A low-visibility signal is how a set silently
  applies fewer settings than the user believes -- the failure the issue calls
  out as the one that decides whether presets stay trustworthy.

### 2. Scope: bound to the specific tool

A set saved from a minimap2 run appears only when configuring minimap2. Not
`alignment` as a run kind.

minimap2 and STAR parameters are not interchangeable, so a kind-scoped set
would be almost entirely drift by construction -- the drift path would fire on
nearly every key, and a mechanism that always fires teaches users to dismiss
it.

### 3. Provenance: record id, name, and revision at apply time

A run records which set configured it, denormalized. See "Provenance" below
for the full field and the reasoning.

### 4. No default sets in v1

Applying a set is always an explicit user action. Nothing auto-applies, and
there is no per-project default. Automatic application of remembered settings
is how a user ends up running something they did not intend -- the same
silent-inconsistency failure this feature exists to prevent, arriving from the
other direction.

### 5. What gets saved: tuning knobs only, derived not listed

A run's `params` mixes tuning knobs (`threads`, `preset`, `sort_memory_mb`)
with run-specific bindings (reference object id, input ids, target node,
project). A set stores only the former.

Saving a reference object id would make applying a set silently re-point a run
at a file from a different project. That is the correctness failure the issue
exists to prevent, inverted.

The exclusion is **structural, not a denylist**: eligible keys are derived
from `spec.fields`, so `reference_id`, input object ids, `target_node`,
`project_id`, and `label` are excluded because they were never `ParamField`s.
A field added to a spec becomes preset-eligible with no second edit.

Two dialog-held values are also out of scope, and the reason differs from the
above:

- **`chunked`** -- a run-shape decision, not a tuning knob. It is not a
  `ParamField` and should not become one for this feature's benefit.
- **Read-group overrides (`rgOverrides`)** -- per-sample metadata, excluded
  for the same reason as input bindings.

### 6. Validation on apply: the tool's schema, not `params_sanitizer`

**The issue's question 5 is based on a misreading and is answered differently
here.** It states that preset values "must go through the same sanitization on
apply as freshly typed ones," naming `params_sanitizer`.

`params_sanitizer` is not input validation. Its own module docstring is
explicit: it is a **disclosure boundary** deciding what is safe to persist
into `JobTiming.params` and eventually upload to an aggregation server, and
"the runners build their tool invocations from `job.payload` directly,
upstream of here and unsanitized." Freshly typed input never passes through
it.

Running set values through `sanitize()` on apply would be actively harmful:
its allowlist is fourteen keys, and `_is_safe_value` rejects any value
containing `/`, `\`, or `~`. Every reference path and most real parameters
would be stripped -- producing exactly the silent under-application that
decision 1 exists to prevent.

**What is done instead:** set values are validated on apply against the tool's
own `ParamField` metadata -- the same schema that backs the form the user
would otherwise type into. This is the honest reading of the issue's stated
intent ("a set must not become a way to store a value that would have been
rejected at entry") using the mechanism that actually does entry validation.

`sanitize()` stays exactly where it is, on the write into `JobTiming.params`
in `executor._record_timing`. It is not called anywhere on the parameter-set
path. A test asserts this (see "Testing").

## Storage model

New model, `backend/app/models/parameter_set.py`. A `TimestampedDocument`, so
it inherits `owner` and is profile-scoped like every other collection.

```python
class ParamSpecFamily(StrEnum):
    ALIGNER = "aligner"
    ASSEMBLER = "assembler"


class ParameterSet(TimestampedDocument):
    name: str
    tool: str                    # "minimap2", "star", "spades" -- the spec key
    family: ParamSpecFamily      # which registry resolves `tool`
    params: dict                 # tuning knobs only
    revision: int = 1            # bumped on params edit, never on rename
```

**`tool` is the specific tool**, per decision 2. Uniqueness is per
`(owner, tool)`, so "sensitive" can exist for both minimap2 and STAR without
collision.

**`family` is stored rather than inferred.** `aligner_registry.spec_for()` and
`assembler_registry.SPECS` are separate lookups with different envelope shapes
(the assembler envelope carries `available` and `unavailable_reason`; the
aligner envelope carries `presets`). Storing the family avoids guessing which
registry owns a tool string at apply time. It grows by one member per tool
family that gains a `ParamSpec`.

**`revision` bumps on a params change only, not on rename.** Provenance asks
"were these runs configured the same?" A rename does not change that answer;
an edit does.

**No `is_default` field**, per decision 4.

**Indexes:**

- `[(owner, tool)]` -- the picker query.
- Unique compound `[(owner, tool, name)]` -- enforces the naming rule at the
  database rather than in a service check that races.

### Deriving eligible keys

```python
def preset_eligible_keys(family: ParamSpecFamily, tool: str) -> frozenset[str]:
    return frozenset(f.key for f in spec_fields(family, tool))
```

Saving intersects the dialog's current `params` with this set. This is
CLAUDE.md's "genuinely derivable" registry pattern, and it carries that
pattern's exhaustiveness test.

## API

New router, `backend/app/api/v1/parameter_sets.py`. Every route takes
`owner: OwnerDep` -- profile scoping is the existing mechanism, not a new one.

```
GET    /api/v1/parameter-sets?tool=minimap2   list, filtered to one tool
POST   /api/v1/parameter-sets                 create {name, tool, family, params}
PATCH  /api/v1/parameter-sets/{id}            rename and/or edit params
DELETE /api/v1/parameter-sets/{id}            delete
POST   /api/v1/parameter-sets/{id}/resolve    apply-time resolution
```

**`?tool=` is required on list.** The picker only ever wants one tool's sets.
Making it optional would create a route returning every set across every tool
-- the route someone later builds a cross-tool picker on, quietly undoing
decision 2.

### `resolve`: a POST that does not mutate

It takes the set id plus the dialog's *current* tool context, and returns the
applied params alongside a per-key verdict.

POST rather than GET because the request carries client context. Server-side
rather than computed in the dialog because the schema and the drift rules are
backend truth -- putting the comparison in the frontend means two
implementations of the drift contract that can disagree.

```json
{
  "applied":  { "threads": 8, "preset": "map-ont" },
  "rejected": [
    { "key": "quality_cutoff", "reason": "unknown_field",
      "detail": "no longer a parameter of minimap2" },
    { "key": "threads_max", "reason": "out_of_range",
      "detail": "16 exceeds the current maximum of 12", "value": 16 }
  ],
  "set": { "id": "...", "name": "Nanopore fast", "revision": 3 }
}
```

### The four rejection reasons

Each derives from a `ParamField` attribute rather than a hand-written rule:

| `reason` | Derived from | Fires when |
|---|---|---|
| `unknown_field` | key not in `spec.fields` | a param was removed or renamed |
| `wrong_kind` | `field.kind` | saved a string where an int is now expected |
| `out_of_range` | `field.min` / `field.max` | bounds tightened since save |
| `invalid_choice` | `field.choices` | a select option was withdrawn |

**This table is decisions 1 and 6 in one mechanism.** The drift check and the
validation check are the same comparison: a saved key either matches the
current `ParamField` or it does not, and the reason it does not is what the
user gets told. There is no separate validator to keep in sync.

### Save

Save is a create against the dialog's current params, not a copy of a
completed run's stored params. When a dialog is opened from a rerun, its
merged `params` *is* that object -- so the issue's verification step ("save a
preset from a completed run's parameters") is satisfied without a separate
run-to-set endpoint.

## UI

**One new component, `ParameterSetPicker`**, rendered in `AlignDialog` and
`AssembleDialog` above the generated fields. It takes the tool and family and
reports applied values through the same `onChange(key, value)` seam the field
renderers already use.

Neither `AlignerParamFields` nor `workflow/ParamForm` changes.

```
Preset  [ Nanopore fast v ]  [Save current as...]  [...]
```

The `...` menu holds rename and delete for the selected set. No new dialog
chrome -- it is a row.

### Apply flow

1. User picks a set -> `POST /{id}/resolve` with current tool context.
2. Every key in `applied` is pushed through `onChange`, filling the form
   exactly as if typed.
3. If `rejected` is non-empty, an inline notice renders **above the fields,
   inside the dialog, and does not auto-dismiss**.

```
Applied "Nanopore fast" -- 4 of 5 settings.
! quality_cutoff not applied -- no longer a parameter of minimap2.
                                                        [Dismiss]
```

**The notice is persistent, not a toast.** This is the detail that decides
whether decision 1 works in practice rather than only on paper. Batch work
means applying the same stale set thirty times, and a notification that
disappears after four seconds is one the user stops reading by sample three.
It is dismissible per-application.

**Applying never blocks submit.** A partially-applied set is a valid starting
point -- the issue is explicit that a preset is "a starting point, not a
lock." The user can edit anything, including a field the set filled.

**Save writes the dialog's current merged `params`**, intersected with
`preset_eligible_keys` -- not the selected set's stored params. So "apply,
tweak two knobs, save as new" does what it looks like. Save prompts for a
name; a collision with an existing set for that tool offers overwrite (which
bumps `revision`) or rename.

### Edit-after-apply tracking

Once a set is applied, the dialog holds the applied id and compares live field
values against what `resolve` returned. Any divergence sets
`edited_after_apply` on the run's provenance record.

Cheap -- a shallow compare on values already in state -- and it is what stops
provenance from claiming thirty runs were identical when one was hand-adjusted
before submit.

## Provenance

One new optional field on `PipelineRun`:

```python
class AppliedParameterSet(BaseModel):
    """Which saved parameter set configured this run, as it read at apply time.

    Denormalized for the same reason `params` and the input names are: a set
    can be renamed or deleted, and a run described only by a dangling id
    stops being describable exactly when the question -- "which of these
    thirty runs used the old settings?" -- is worth asking.
    """

    set_id: PydanticObjectId
    name: str                  # as it read at apply time, not as it reads now
    revision: int              # the set's revision when applied
    edited_after_apply: bool   # user changed a filled field before submitting


from_parameter_set: AppliedParameterSet | None = None
```

`None` means the run was configured by hand, which stays the common case and
costs nothing.

**The denormalization is the feature, not an optimization.** Storing only
`set_id` would make a renamed or deleted set erase the provenance of every run
that used it -- rendering the exact question provenance exists to answer
unanswerable. `PipelineRun` already reasons this way: the comment on `params`
notes that jobs are TTL-pruned after 30 days and a run described only by its
jobs "stops being describable exactly when a record of what was run is most
valuable." Same argument, same model.

**`revision` and `edited_after_apply` together are what make the batch
question answerable.** Thirty runs sharing `(set_id, revision)` with
`edited_after_apply: false` were genuinely configured identically. One with
`revision: 2` used the settings before an edit; one with `edited_after_apply:
true` was hand-adjusted. Without both fields you can group runs but cannot
tell whether the grouping means anything -- worse than no provenance, because
it would look authoritative.

**Written at run creation in `run_service.py`**, the single `PipelineRun(...)`
construction site. The launch request carries the applied-set context from the
dialog; if absent the field stays `None`. No other write path to keep in sync.

**Surfaced in the run detail view** as read-only text:
`Preset: Nanopore fast (rev 3, edited)`. If the set was since deleted the line
still renders from the stored snapshot. Clicking through to the set is a
reasonable follow-up, not v1.

## Testing

Backend, under `backend/tests/`. Run from the worktree with
`./backend/run-worktree-tests.sh tests/ -q` -- not `docker compose exec api`,
which would silently test main's code (CLAUDE.md, "Verifying changes").

- **Exhaustiveness.** `preset_eligible_keys` derives from `spec.fields` for
  every member of both registries, in the shape CLAUDE.md's "genuinely
  derivable" section prescribes. This is the test that catches the
  STAR/`_SIDECAR_ROLES` class of failure.
- **The four rejection reasons, one test each**, driven by a spec whose fields
  were mutated after save: a removed key, a tightened `max`, a withdrawn
  choice, a changed `kind`. This is the issue's second verification item, made
  mechanical.
- **Round-trip.** Save from a run's params, apply to a new run, assert the
  resulting run's stored params match the original for every eligible key.
  The issue's first verification item.
- **Exclusion.** A run whose params include `reference_id` and input ids
  produces a set containing neither. Asserted positively, not by
  absence-of-crash.
- **`sanitize()` is not on the apply path.** Assert `resolve` returns values
  containing `/` and keys outside `ALLOWED_KEYS` unmolested. This is the
  regression guard against someone re-reading the issue's question 5 and
  "fixing" it back.
- **Uniqueness.** Same name + same tool + same owner collides; same name
  across two tools does not.
- **Provenance.** A run launched with an applied set carries
  `from_parameter_set` with the name as it read at apply time; renaming the
  set afterwards does not change the stored value.

**Frontend: manual.** There is no headless component-testing setup in this
repo and none is expected (CLAUDE.md). Verification is at the worktree stack
-- `./ops/worktree-up.sh`, UI on 5273 -- checking that a stale set shows the
persistent notice, that the notice survives a second application, and that
editing a filled field before submit records `edited_after_apply`.

## Follow-ups, not in v1

- Give `trim_reads` a `ParamSpec`, which lights up parameter sets for it with
  no further work here.
- Click-through from a run's provenance line to the parameter set.
- Per-project default sets (decision 4 declines this for v1).
