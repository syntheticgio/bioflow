# Paired-read detection beyond filenames — implementation plan

**Date:** 2026-08-05

**Issue:** [#17](https://github.com/syntheticgio/bioflow/issues/17)

**Spec:** [`docs/superpowers/specs/2026-08-05-paired-read-detection-design.md`](../specs/2026-08-05-paired-read-detection-design.md)

The spec records no open questions. This plan turns it into an ordered build.
Where this plan makes a call the spec did not, it is marked **[plan decision]**.

## Shape of the change

Six phases, each independently committable and independently revertable. Phases
1--2 are the feature. Phases 3--4 are the two defects the spec found, which are
unrelated to each other and to the feature. Phase 5 is the vocabulary
extension, deliberately last because it is the only part with no real-data
evidence behind it. Phase 6 verifies against the running app.

**[plan decision]** The defects ship *after* the feature rather than before,
even though they are smaller. Both touch the SRA ingest path, and the feature
touches the generic ingest path; landing the feature first keeps the diff that
changes pairing behavior separate from the diff that changes SRA behavior, so a
regression in either is attributable.

## Phase 1 — the pure decision function

**Files:** `backend/app/pipelines/pairing.py`,
`backend/tests/pipelines/test_pairing.py`

`pairing.py` imports only `re` today and must keep importing nothing that knows
about Motor, `DataObject`, or the filesystem. Everything here is pure.

Add:

```python
@dataclass(frozen=True)
class PairInput:
    name: str
    facts: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)


class Verdict(StrEnum):
    CONFIRMED = "confirmed"
    NAME_ONLY = "name_only"
    REJECTED_LAYOUT = "rejected_layout"
    REJECTED_READ_IDS = "rejected_read_ids"
    NO_MATCH = "no_match"
```

Helpers, each separately testable:

- `_first_token(read_id: str) -> str` — text up to the first whitespace. This is
  the function that makes `length=150` vs `length=149` a non-issue; it carries a
  comment saying so with the real header as the example, because the next person
  to "simplify" it into a whole-string compare needs to see why not.
- `_run_field(read_id: str) -> str` — the leading structural field: text before
  the first `.` for SRA-style, before the first `:` for Illumina-style. Stable
  across every read in a run, which is what makes it safe to veto on.
- `read_ids_agree(a, b) -> bool` — first-token sets intersect.
- `read_ids_conflict(a, b) -> bool` — every run field on one side differs from
  every run field on the other. **Not** `not read_ids_agree(...)`; the spec's
  finding 4 is that these are different questions and conflating them unpairs
  legitimately filtered mates.

Then `verdict(a: PairInput, b: PairInput) -> Verdict`, in this order:

1. `is_mate_of(a.name, b.name)` false → `NO_MATCH`. Cheapest, and the common
   case.
2. Either `metadata.get("read_type") == "single-end"` → `REJECTED_LAYOUT`.
3. Both sides have non-empty `facts["first_read_ids"]`:
   - `read_ids_conflict` → `REJECTED_READ_IDS`
   - `read_ids_agree` → `CONFIRMED`
   - neither → `NAME_ONLY`
4. Otherwise → `NAME_ONLY`.

**[plan decision]** Order matters and is asserted by test: the layout veto runs
before the read-ID check so a single-end file with no `first_read_ids` is still
rejected, and `NO_MATCH` precedes both so the expensive-ish list work never runs
for the overwhelmingly common non-candidate.

**Tests.** The spec's table, verbatim, using the literal strings pulled from the
database — including `length=150`/`length=149` on the two `ERR17609896` headers
and the identical `SRR39891651.1 1 length=2882` on the derivative pair. Add a
test named for the trap: a hand-built fixture that put the same `length=` on
both mates would pass an implementation that rejects every real pair, so the
real strings are the test's whole value.

Green gate: `./backend/run-worktree-tests.sh tests/pipelines/test_pairing.py -q`

## Phase 2 — both call sites consume it

**Files:** `backend/app/queue/results.py`,
`backend/app/services/pipeline_service.py`, tests for each

Neither call site needs a new query: both already load full `DataObject`s, and
`facts`/`metadata` are plain dicts on the model.

`results._link_mate` — replace

```python
matches = [c for c in candidates if pairing.is_mate_of(obj.name, c.name)]
```

with a verdict pass that keeps only `CONFIRMED`/`NAME_ONLY`, and logs each
rejection with its reason (`mate_rejected`, with `verdict=` and both names).
The existing `len(matches) != 1` ambiguity handling is unchanged and still runs
after — a veto reduces the candidate set, it does not change what ambiguity
means.

`pipeline_service.suggest_mate` — the same filter over its own candidate loop.

**[plan decision]** `suggest_mate` returns `None` on a veto rather than
returning the object with a flag. The dialog then shows no auto-suggestion and
the user pairs manually, which is the existing, already-working path. Plumbing a
reason to the UI is a larger change than this issue needs, and the log line
carries the diagnosis for anyone asking why.

**Tests.** For `_link_mate`: a single-end name-matching pair is not linked; a
conflicting-ID pair is not linked; a real-shaped valid pair still is; both
signals absent still links (fast path). For `suggest_mate`: the vetoed pair
returns `None`.

## Phase 3 — defect: `read_number` on the SRA path

**Files:** `backend/app/queue/results.py`, test

```python
await r1.set({DataObject.mate_object_id: r2.id})   # today: no read_number
```

Set `read_number` 1 and 2 alongside, matching what `_link_mate` already does.
`_describe` has already labelled these `R1`/`R2`, so the values come from the
same source that decided the link and cannot disagree with it.

Also correct `object.py:240`'s comment: nulls are not only "pairs predating this
field", they are every pair the SRA path ever created.

**Backfill.** A one-shot script in the shape of
`backend/scripts/fix_legacy_component_roles.py`: for every object with
`mate_object_id` set and `read_number` null, derive from
`pairing.split_mate(name)` and set it; skip where the name yields nothing.
Exactly 2 objects qualify in the current database, which makes this verifiable
by eye.

## Phase 4 — defect: the false comment

**Files:** `backend/app/queue/results.py`

Replace the claim that `` `<acc>_1.fastq` is not a shape its R1/R2 convention
detects `` with the true reason: fasterq-dump has already labelled which file is
which, so the link is known rather than inferred, and no inference should
override it. Record that the tokens *are* detected (with the verified
`split_mate` output) so the next reader does not re-derive it.

**[plan decision]** Comment-only, no behavior change, its own commit. The bypass
stays.

## Phase 5 — vocabulary extension

**Files:** `backend/app/pipelines/pairing.py`, `backend/tests/pipelines/test_pairing.py`

Add `_fwd`/`_rev` and `_forward`/`_reverse` to `_MATE_TOKENS`, longest-first as
the existing ordering comment requires (`_forward` before `_fwd` before `_f`
would matter if `_f` existed; it does not, and is not being added). Give them
scheme `"F"` in `_SCHEME` so they cannot cross-pair with `R` or `N` names.

Bare `.1`/`.2` is excluded — the spec says why.

**Tests.** `foo_fwd.fastq.gz`/`foo_rev.fastq.gz` pair; `foo_fwd`/`foo_2` do not
(scheme guard); a file named `refwd.fastq` is not mangled (the token is matched
at the end of the stem, and the existing `sample_R1_run_2` test covers the
general form of this).

## Phase 6 — verify against the running app

Unit tests cannot show that the facts a real ingest writes are the facts
`verdict()` reads. The spec's whole premise is that `first_read_ids` and
`read_type` are already populated, and that was measured on the *current*
database, not on a fresh ingest.

From this worktree:

```bash
./ops/worktree-up.sh          # UI 5273, API 8100, its own database
```

Then, against the worktree stack:

1. Upload a `.fastq.gz` pair — the compressed case with no real-data backstop.
   Confirm `first_read_ids` lands and the pair links with `verdict=confirmed`.
2. Download a paired SRA run. Confirm `read_type='paired-end'`, the mates link,
   and **`read_number` is now 1 and 2** (Phase 3).
3. Upload two unrelated single-end files renamed to `x_1.fastq`/`x_2.fastq`.
   Confirm they do not link and `mate_rejected` names the reason.
4. Confirm the launch dialog still offers manual pairing for case 3 — the
   issue's fourth acceptance criterion, and the thing a veto must not break.

**[plan decision]** Case 3 is constructed by hand because the database has no
natural instance. That is the finding, not a gap: the collision the TODO entry
worried about has never actually occurred here, which is why this issue is
`priority:low`.

Note `/data` is shared with the main stack — these are uploads and downloads
into a worktree project, not rewrites of an existing artifact, so this is the
safe side of that tradeoff.

## Test gate

Full suite from the worktree, reading the count rather than the exit code:

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Baseline to beat: the tree's current count. Anything less than "all passed" is
not green, and a worktree run must use this script — `docker compose exec api`
from here would test main's code and report a result about the wrong tree.

## Closing out

Per CLAUDE.md, in the same commit or the one after the last phase lands:

- Append ` — FIXED` to the `Mate detection is filename-only` heading in
  `docs/TODO.md`, note what shipped and where, and **say what the
  implementation did differently**. The delta is large here and is the most
  valuable thing to record: the entry assumed this work required decompressing
  two files, and it required opening none, because the check had been computed
  at ingest and thrown away since before the entry was written.
- Move the entry in full to `docs/TODO-done.md`.
- Record that `read_type` was a third signal the entry never considered.
