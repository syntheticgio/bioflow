# Prior runs on pipeline suggestion cards

**Date:** 2026-08-03
**Status:** Approved, not yet implemented

## Problem

The Actions tab's suggestion cards offer a pipeline to run on a file, but say
nothing about whether that exact pipeline has already been run on that file.
The user has no way to tell, from the card, that they already aligned this
FASTQ against this reference last week -- or, worse, that they launched it
twice and it failed both times. The second case is the one that costs
something: a card that hides its failures invites the same failed launch
again.

## What ships

A card that has prior runs grows a list of them between its description and
its launch button, and its button reads "Launch again". A card with no prior
runs renders exactly as it does today.

```
ALIGN                                    2 PRIOR RUNS

bwa-mem2 -> BAM

Align to GCF_000146045.2_R64_genomic.fna, sort and index.
--------------------------------------------------------
Aug 1    SRR39891651.trimmed.bam            Succeeded
Jul 28   SRR39891651.bam                    Partial
--------------------------------------------------------
                    [ Launch again ]
```

Up to three runs, newest first.

## Decisions

### Which runs belong to a card

A run is a prior run of a card when **this object was one of its inputs** and
**its parameters match what the card would launch**. Not "any run of this
kind": the card is a specific offer, and a list that mixed in alignments of
other files against other references would not be a record of this button.

Hand-launched runs qualify. They are matched on their recorded data, not on a
"came from a card" flag, so a run launched through the Computations dialog
with the same parameters is the same prior run -- which is what the user
means by "have I done this before".

### How the match is made

Structurally, at query time: compare the card's launch body against each
candidate run's stored `params` and `inputs`. No new field, no migration, and
runs already in the database appear immediately.

The alternative -- hashing the launch body into a stored `PipelineRun`
signature -- was rejected on retroactive coverage. It would ship a feature
that says "no prior runs" on every card until the user re-launches
everything, which is exactly when the feature is least convincing. It also
fails silently the first time a default changes in `pipeline_service`.

The cost accepted in exchange: the launch body and `params` are not the same
shape, so the comparison is per-kind and hand-maintained. It lives in one
explicit table (see below) rather than being spread across the card builders.

### What each row shows

Date, the run's output files as links, and the run's derived status.

**One row per run, listing every non-sidecar output.** A paired trim shows
both R1 and R2 on its row; an alignment shows its BAM and not its `.bai`.
One row per *output object* was rejected because it breaks the "N prior
runs" count against the number of rows on screen; showing only a primary
output was rejected because it silently drops R2.

**Failed runs are listed.** They have no output to link, so the row is date
and status alone. This is the case that motivates the feature as much as the
success case does.

**Status appears on every row, and file size appears nowhere.** Size was
considered -- an output whose size changed unexpectedly is a real signal --
but it is a weak one next to knowing the run failed, and two numbers on a row
whose job is to say "this already happened" is one too many.

| Run status | Row |
| --- | --- |
| Succeeded | `Aug 1 · SRR39891651.trimmed.bam · Succeeded` |
| Partial | `Aug 1 · SRR39891651.bam · Partial` |
| Failed | `Jul 28 · Failed` |

Runs still waiting or running are omitted: the card is a record of what has
happened, and the Activity view already owns work in flight.

An output object that has since been deleted renders as plain text rather
than a dead link. The run still happened; only the file is gone.

## Backend

### `SuggestionCard.prior_runs`

A new field on the frozen dataclass, defaulting to an empty list, carried
through `as_dict`:

```python
prior_runs: list[dict] = field(default_factory=list)
```

Each entry:

```python
{
    "run_id": str,
    "finished_at": datetime | None,   # newest-first sort key
    "status": "succeeded" | "partial" | "failed",
    "outputs": [{"object_id": str, "name": str, "exists": bool}],
}
```

`outputs` is empty for a failed run. `exists` is false for an output that has
been deleted, and its `name` is the run's record of the name rather than a
live lookup.

### `attach_prior_runs(cards, obj, *, owner)`

A new function in `suggestion_service`, called by `suggestions_for` after the
cards are built. It runs once for all cards, not once per card:

1. Query `PipelineRun` for this project where `inputs.object_id == obj.id`.
2. `run_service.status_for_many` for their derived statuses -- already
   owner-scoped and already two queries rather than 2N.
3. Query `DataObject` once for every id in every candidate run's `outputs`,
   to resolve names and existence.
4. Per card, filter the candidates by kind and by the parameter comparison,
   drop waiting/running, sort newest first, take three.

Two extra queries in total regardless of card count.

Ordering is by the run's finish time where it has one, falling back to
`created_at`. A `PipelineRun` has no `finished_at` of its own -- it is
derived from member jobs -- so the implementation should use `created_at`
unless resolving a real finish time is free at the point the statuses are
already being computed. Displaying a launch date is honest either way; what
matters is that the sort and the displayed date agree.

### The parameter comparison

One module-level table naming the fields that distinguish two runs of a kind:

```python
_MATCH_FIELDS: dict[RunKind, tuple[str, ...]] = {
    RunKind.ALIGNMENT: ("aligner",),
    RunKind.TRIM: ("tool",),
}
```

Those two are the entries this feature needs on day one: alignment and trim
are the card kinds with a parameter that genuinely distinguishes one run from
another. Every other kind starts absent, and a kind absent from the table
matches on kind alone. That is the deliberate
default: an unlisted kind over-matches rather than showing nothing, and
over-matching is visible while under-matching is silent.

Two traps this table has to respect, both verified against the code:

- **The reference is not in `params`.** An alignment run records its aligner
  in `params` and its reference as an entry in `inputs` with role
  `REFERENCE`. Matching the card's `reference_id` therefore means reading
  `inputs`, not `params`. A comparison that only walked `params` would treat
  alignments against two different genomes as the same prior run.
- **`params` holds more than parameters.** An alignment's `params` includes a
  `read_group` dict built partly from the object's own name. Comparing
  `params` wholesale would make almost nothing match. Only the named fields
  are compared.

`RunKind.TRIM` keys on `tool`, which `create_run` stores in the run's `tool`
field rather than in `params` -- the comparison reads whichever location the
kind actually uses, which is why the table's values are field names
interpreted per kind rather than blind `params` keys.

## Frontend

`PipelineSuggestions.tsx` gains the prior-runs block; `PipelineSuggestion` in
`api/types.ts` gains the matching optional field.

- `PRIOR RUNS` count marker in the card's top-right, opposite the category.
- A bordered list between description and button: date, output links, status.
- Output links navigate to `?sel=object:<id>`, the same pattern
  `ActivityView` already uses to open a file in the explorer's detail panel.
- Button label is "Launch again" when `prior_runs` is non-empty, "Launch"
  otherwise.
- `prior_runs` empty or absent renders today's card unchanged.

The component stays a renderer: no filtering, no sorting, no status
derivation. Everything it displays is decided server-side, which is the
existing contract for these cards.

## Testing

**Backend**, in `backend/tests/services/test_suggestion_service.py`:

- A run with matching params appears on the card.
- A run with a different aligner does not.
- An alignment against a different reference does not (the `inputs` trap
  above).
- A failed run appears, with empty `outputs`.
- A running run does not appear.
- More than three matching runs yields exactly three, newest first.
- A run whose output object was deleted yields `exists: false` and keeps its
  recorded name.
- A card with no matching runs has `prior_runs == []`.

**Against the real database**, per CLAUDE.md: the last two suggestion-rule
bugs were both things hand-built fixtures could not expose, because the
fixtures already looked the way the rules expected. A
`docker compose exec api python -c ...` check against a real project with
real alignment runs, confirming the card's prior runs are the runs that
actually produced its files.

**Frontend**: manual, at localhost:5273 via `./ops/worktree-up.sh`. There is
no headless component-testing setup in this repo and none is expected. Check
all four states -- no prior runs, succeeded, partial, failed -- and that an
output link opens the right file.

## Out of scope

- Re-running with the *same* outputs, or any form of caching. "Launch again"
  launches again.
- Prior runs on the Computations dialogs. This is a suggestion-card feature.
- Any change to what a run records at launch time.
