# Open-vocabulary metadata fields

Written 2026-08-06 for GitHub issue
[#66](https://github.com/syntheticgio/bioflow/issues/66), which was split out of
[#61](https://github.com/syntheticgio/bioflow/issues/61) and deferred by
[`2026-08-06-sam-platform-enum-design.md`](2026-08-06-sam-platform-enum-design.md)
as "the only part with a user-visible decision in it."

That decision turned out to rest on a premise the data does not support. This
spec records what the rows actually say, declines both remedies the issue
proposed, and fixes the defect one level down — in the widget, where it is
shared by three other fields measurably suffering from it today, and four more
built the same way.

## What the data says

The issue frames the defect as a *disagreement* between two vocabularies: a
dropdown offering family names (`PacBio`, `Oxford Nanopore`) against SRA
enrichment writing instrument models (`PacBio RS`, `MinION`), fragmenting
`metadata.platform` across both.

Queried against the real database, the disagreement is one-sided. All 28
objects carrying `metadata.platform` hold instrument models:

```
Illumina HiSeq 4000, Illumina MiSeq, Illumina NovaSeq X Plus,
MinION, NextSeq 550, PacBio RS, Sequel IIe
```

Not one holds a family name. `Illumina MiSeq` is the sole value that also
happens to be a dropdown entry. **The family-name vocabulary has no users**, so
the fragmentation the issue describes has never occurred and there is nothing
to migrate away from.

The issue also undercounts: it says ten objects, and it is 28 rows over 7
distinct values — every one of them tripping the spurious warning.

### Why the dropdown has no users

`SchemaMetadataEditor.tsx:281` renders `FieldType.ENUM` as a hard `<select>`.
There is no free-text path. A user who wants to record `NextSeq 550` by hand
**cannot** — the only reason those values exist at all is that SRA enrichment
writes them around the form.

So the current design prevents the user from entering the values the system
itself treats as authoritative, then warns about them once they arrive by
another route. The `<select>` already carries a fallback `<option>` for
off-list stored values, whose comment ("must remain selectable, or saving the
form would silently discard it") is the existing design apologizing for exactly
this.

### The problem is not confined to `platform`

The same query over the other list-backed fields:

Measured by running `coerce_and_validate` over the real objects — 51 objects
carry at least one of these four fields, and they produce **53 warnings**:

| Field | Warnings | Values warning | Values not warning |
|---|---|---|---|
| `platform` | **31** | all 7 instrument models | none |
| `organism` | **15** | *Lycoris aurea*, *S. cerevisiae S288C*, *S. aureus* str. Newman, *T. brucei brucei*, *T. brucei brucei* TREU927 | *E. coli*, *S. cerevisiae* |
| `reference_build` | **6** | `ASM231043v1`, `ASM244v1`, `R64` | none |
| `assay` | **1** | `OTHER` | `Amplicon`, `ChIP-seq`, `WGS` |

`reference_build` has `platform`'s exact shape: NCBI assembly accessions
against a list offering GRCh38 and GRCm39, zero hits, every row warning. A fix
scoped to `platform` would leave it untouched.

`organism` is the instructive middle case. Its vocabulary is *partially* live —
*E. coli* and plain *S. cerevisiae* are dropdown entries and pass clean, while
strain-qualified names from NCBI (*S. cerevisiae* **S288C**) warn. No closed
list can absorb strain suffixes; this is an open vocabulary that merely looks
half-closed, and it is why the fix keeps the suggestions rather than replacing
them.

Note on `reference_build`: the key appears four times in the schema, twice as
an ENUM (alignment, variant) and twice as plain free text (reference,
intervals), resolved per object by `coerce_and_validate`'s scoped-definitions
rule at `schemas.py:508`. The six warnings above are on alignment and variant
outputs, which get the ENUM spec. Only the two ENUM copies need the flag; the
free-text copies are already open and are left alone.

The stored `assay` value `'OTHER'` is worth one clarifying note, because it
invites a wrong inference. It is **SRA's own literal `LIBRARY_STRATEGY`
value**, preserved verbatim by `_map_strategy`, which passes unrecognized
strategies through unchanged rather than synthesizing a placeholder. That is
correct behaviour and nothing here changes it. It reinforces the design: these
fields receive externally-owned vocabularies as-is, which is why they must be
open.

## Options considered

The issue offered three. All three are declined in favour of a fourth.

1. **Leave it.** Rejected: 53 warnings across 51 objects and four fields, each
   one wrong about which value is authoritative.
2. **Widen the `platform` dropdown to instrument models.** Resolves one
   field's symptom, still a closed `<select>` the moment a new instrument
   ships, and does nothing for `organism` or `reference_build`.
3. **Split `platform` / `instrument_model`** — the 2026-08-05 spec's
   recommendation. Rejected on the data: it was written before anyone read the
   rows. Its closed `platform` field would be populated *entirely by
   derivation*, since no user has ever entered a family name. That buys a
   migration, a dual read path across five call sites (`_qc_platform`,
   `default_read_group`, `default_library`, `is_short_read`, `is_long_read`),
   and a second field, to store a value `sam_platform()` already computes on
   demand from the first.
4. **Make open-vocabulary fields free-text-with-suggestions.** Chosen. One
   widget change plus a schema flag; no migration, no dual read path, and it
   fixes `organism`, `reference_build`, and five others at the same time.

The schema already believes these vocabularies are open. Its module docstring
says so ("arbitrary keys remain allowed, because no fixed vocabulary survives
contact with a real lab"), the warning text says "stored anyway," and the
`<select>` carries a fallback option for off-list values. **The widget is the
only part that disagrees.** Option 4 removes the disagreement rather than
negotiating with it.

## Design

### `FieldDef.open_vocabulary`

One new field:

```python
open_vocabulary: bool = False
```

Set `True` on the eight fields whose lists end in the `"Other"` sentinel —
`organism`, `assay`, `library_prep`, `platform`, `reference_build` (both
copies, in `ALIGNMENT_FIELDS` and `VARIANT_FIELDS`), `aligner`,
`variant_caller`, `interval_type`.

The `"Other"` sentinel is **dropped from each of those lists** in the same
edit. With free text available, storing the literal string `"Other"` is
strictly worse than storing the real answer, and today it is a selectable
option that discards information.

`to_dict()` gains `"open_vocabulary": self.open_vocabulary`.

The six remaining ENUM fields stay closed and are unchanged: `sex`,
`read_type`, `mate`, `variant_type`, `assembly_level`, `sequence_type`. These
are genuinely complete sets — `sequence_type` derives from the `SequenceType`
enum, `assembly_level` is NCBI's own fixed four.

**Inclusion rule**, to live as a comment on the field: *a list is open if its
values come from outside this repo (NCBI, an instrument vendor, a lab's own
kit names); closed if this repo or a published spec defines the complete set.*

This flag is a hand-maintained per-field property, the shape `CLAUDE.md` warns
about. Per that document's three-way split it is the **middle** case —
intentionally partial, where forcing coverage would be wrong. A test demanding
every field be open, or every enum member reachable, would be a detector that
starts guessing. The written inclusion rule above is the control, plus the
disjointness test below. No exhaustiveness test is proposed, deliberately.

### The warning becomes conditional

`schemas.py:550` suppresses the "not one of the suggested options; stored
anyway" warning entirely when `open_vocabulary` is set. Off-list values on
closed fields still warn — and that warning now means something, because it
fires only where the list claims to be complete.

This is what clears all 53 measured warnings.

### The widget

`SchemaMetadataEditor.tsx:280`'s two-way branch becomes three-way:
`enum && open_vocabulary` → combo; `enum` → `<select>` (unchanged); else the
existing input.

The combo follows the shape already established three times in this codebase
(`ModelCombo.tsx`, `DifferentialExpressionDialog.tsx:264`,
`SearchView.tsx:310`): `<input list={id}>` over a `<datalist>` of the options.
`ModelCombo`'s docstring already argues this exact case — "a model id the user
knows is valid must not be blocked by a listing endpoint having a bad day."

**Not reused directly.** `ModelCombo` hardcodes `settings-input` styling and a
models-specific placeholder and hint, while the metadata editor's inputs carry
their own inline styles to match the surrounding form. Parameterizing both away
would leave nothing shared but a six-line datalist idiom. A local branch in the
same file, commented to name `ModelCombo` as the precedent, is the smaller
change.

The datalist id must be per-field — `meta-${field.key}-options`. Several combos
render on one form, and a duplicated id silently binds every one of them to the
first field's suggestions.

The fallback `<option value={str}>` at line 294 is deleted; in a combo the
stored value is simply the input's value. Its comment's *concern* moves to the
combo branch, restated as why the field is a combo at all.

`api/types.ts`'s field-definition type gains `open_vocabulary: boolean`.

### The trade this makes

A combo accepts anything, including typos. `Illumina NovaSq` will save silently
where the `<select>` made it unrepresentable.

That is a real loss, not a pure win, and it is accepted knowingly: the
`<select>` was preventing the *correct* values while the warning fired on all
of them. Guarding against typos at the cost of the true answer is the wrong
side of that trade for a single-user local tool whose schema module already
declares its fields to be suggestions rather than restrictions.

## Tests

In `backend/tests/metadata/`, alongside the existing schema tests. Run from the
worktree with `./backend/run-worktree-tests.sh tests/ -q` — **not** `docker
compose exec api`, which tests main's code.

- **Open fields suppress the warning.** `coerce("platform", "NextSeq 550")`
  returns the value with a `None` warning. This encodes the issue's complaint.
- **Closed fields still warn.** `coerce("read_type", "triple-end")` still
  warns. This is the direction that fails if the conditional is inverted;
  per `CLAUDE.md`, the assertion that flips *off* is the one that proves the
  seam, since the other passes whether or not the branch was reached.
- **No `"Other"` survives** in any open field's options, and no open field's
  list is empty after the removal.
- **Disjoint and total.** Every ENUM `FieldDef` is either `open_vocabulary` or
  in the closed set, and none is both. This is a completeness check over the
  field list itself — derivable, unlike the inclusion rule — so it is worth
  pinning.
- **`to_dict()` carries `open_vocabulary`.** Cheap, and it is the wire that
  breaks silently.

### Real-database check

Required by `CLAUDE.md`'s "check a rule against the real database": the unit
tests above feed hand-built `FieldDef`s that already look how the rules expect,
which is precisely the failure that let the Actions-tab suggestion rules pass
green while being wrong.

So, against real objects, re-run the measurement from "What the data says" and
assert the count goes from **53 warnings to 0** across `platform`, `organism`,
`reference_build`, and `assay`. That is the check that catches a field nobody
remembered to flag — and the before-number makes it verifiable later.

The query, for reuse: iterate objects carrying any of the four keys, call
`coerce_and_validate(metadata, format["kind"], role)` on each, and count
warnings whose `key` is one of the four. Passing `format` itself rather than
`format["kind"]` raises `TypeError: unhashable type: 'dict'` — `format` is a
nested document on the object, not a scalar.

### Manual verification

At the worktree stack (`./ops/worktree-up.sh`, UI on 5273):

- A file with `metadata.platform = 'MinION'` renders as a combo, value intact,
  family names still offered as suggestions.
- Typing a new instrument model saves it, with no warning shown.
- A closed field (`read_type`) still renders as a `<select>`.

## Out of scope

Recorded so a later reader does not think these were overlooked.

- **No migration.** Nothing moves and no stored value changes meaning. This is
  the central reason this approach beats the issue's option 3.
- **No change to `sam_platform()`** or any of its five read paths. They consume
  instrument models correctly today; the substring funnel's own comment says it
  was built for exactly that, because "SRA enrichment writes INSTRUMENT_MODEL,
  so a real file says 'NextSeq 550' and never 'Illumina NextSeq'."
- **The `platform` option list keeps its family names** (minus `"Other"`).
  They are now suggestions in a combo, which is a reasonable thing for them to
  be. Widening them to instrument models is a separate judgment call this
  change does not need to make.
- **`_map_strategy` is unchanged.** Its pass-through of unrecognized SRA
  strategies is correct.
