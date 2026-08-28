# Chunked alignment treats enqueue's return as a job id — design

Date: 2026-08-25.

Closes [#851](https://github.com/syntheticgio/bioflow/issues/851). Found while
reading the chunked-alignment path for
[#835](https://github.com/syntheticgio/bioflow/issues/835).

A bug, not a feature. This spec is diagnostic: it establishes what actually
happens today, because the ticket's own account of the failure turns out to be
too generous, and settles the one design question the fix depends on.

## What exists today

Verified against this worktree on 2026-08-25.

`backend/app/queue/chunked_align_handlers.py:56-61`:

```python
sid = await queue.enqueue(
    "align_reads",
    payload=sub_payload,
    owner=owner,
    parent_job_id=ctx.job_id,
)
sub_job_ids.append(sid)
```

`enqueue` is declared `-> Job | None` (`backend/app/queue/queue.py:56-75`) and
returns the `Job` object (`:167`) or `None` when `_handle_dedup` reported a
duplicate (`:154-155`). It has never returned an id.

Those values are used as ids at `:86`, `await Job.get(jid)`, inside a bare
`except Exception: continue` at `:84-87`. They are also returned to the applier
in the handler's result dict, `:121` `"sub_job_ids": sub_job_ids`.

Retry configuration: the handler declares `max_attempts=1` (`:32`), and the
polling loop raises `RetryableError` on both a failed bucket (`:96-99`) and the
24h deadline (`:113-116`, `deadline = time.time() + 86400` at `:79`).

## Decision Q1: the normal case is broken too — this is total, not incidental

The ticket describes the normal case as working by coincidence: "whether
`Job.get(<Job>)` happens to work is incidental." It does not work.

`Document.get` (beanie/odm/documents.py:248-278) does:

```python
if not isinstance(document_id, extract_id_class(...)):
    document_id = parse_object_as(get_field_type(...), document_id)
```

A `Job` is not a `PydanticObjectId`, so it falls to `parse_object_as`, which
raises a Pydantic `ValidationError`. Verified empirically in the running `api`
container:

```
TypeAdapter(PydanticObjectId).validate_python(<arbitrary object>)
→ RAISES: ValidationError
```

So on **every** poll, for **every** bucket, `Job.get(jid)` raises, the bare
`except` at `:84-87` swallows it into a `chunked_sub_job_lookup_failed` warning,
and `continue` skips the bucket. `completed` never leaves 0. `failed` is never
set. The loop sleeps 5s and repeats until `deadline`.

**Chunked alignment cannot succeed at all.** Not in the dedup case — in every
case. Every chunked alignment ever launched has burned 24 hours of polling while
its buckets ran and succeeded underneath it, then died.

Two things kept this hidden.

- **The bare `except`** turns a `ValidationError` — a programming error with a
  precise message — into a per-bucket warning that reads like a transient
  lookup miss.
- **The tests mock the bug away.** `backend/tests/queue/test_chunked_align_orchestrator.py:64-72`'s
  `_enqueue` fixture returns `str(PydanticObjectId())`, i.e. a string id, which
  is what the code *assumes* `enqueue` returns rather than what it does. The
  suite is green and asserts the fan-out is correct
  (`test_all_succeeded_returns_the_merge_inputs`, `:136-138`) against a queue
  that does not behave like the real one. This is the more important finding for
  the fix: correcting the handler without correcting the fixture leaves the
  tests unable to fail on a regression.

**The applier does not share the bug.** `results.py:_apply_align_reads_chunked`
(`:1441-1535`) does `Job.get(PydanticObjectId(jid))` at `:1465` — an explicit
coercion that works on a string and raises on a `Job`. Its own bare `except`
(`:1466-1469`) appends to `unresolved` rather than silently skipping, and its
every-bucket-or-none refusal (`:1491-1509`, the #595 comment) raises
`PermanentError`. So the applier would refuse correctly. It is simply never
reached, because the orchestrator never returns.

The applier's contract is therefore the fix's target: **`sub_job_ids` must hold
strings that `PydanticObjectId(...)` accepts.**

## Decision Q2: a dedup here is unreachable — treat `None` as a bug, not a case

The ticket asks whether a deduplicated sub-job should be tracked, failed, or
counted complete. Tracing the dedup mechanism answers a prior question: it
cannot happen.

`enqueue` builds the stored key at `queue.py:143-146`:

```python
stored_dedup_key = f"{owner}:{dedup_key}" if dedup_key is not None else None
```

It is **not derived from the payload**. It is the caller's `dedup_key`
parameter, folded with `owner`. When the caller passes none, the stored key is
`None` — and the comment at `:141-143` is explicit about why it is left `None`
rather than defaulted to the bare owner string: "the unique index's `$type`
clause exempts missing keys, and turning 'no deduplication' into the bare owner
string would collide every opted-out job in a profile with every other."

The index (`backend/app/models/job.py:235-243`) is a unique partial index over
non-terminal states with `"dedup_key": {"$type": "string"}` in its partial
filter. A `None` key is not a string, so the document is outside the index and
`DuplicateKeyError` cannot fire.

`chunked_align_handlers.py:56-61` passes **no `dedup_key`**. Therefore
`stored_dedup_key` is `None`, the insert is exempt from the index, and
`_handle_dedup` cannot return `None` for these sub-jobs.

(For contrast, `pipeline_service.py:2545-2570` — the non-chunked align launch —
does pass `dedup_key=dedup_key`. Chunked sub-jobs deliberately do not, which is
right: two buckets of one alignment must both exist.)

**So the design question the ticket wanted settled resolves to: there is no
dedup case to handle.** The fix does not need a policy for tracking an existing
in-flight job, because one cannot be found — and inventing that policy would be
writing an unreachable branch and a test that only passes because it mocks a
condition the queue cannot produce.

What the code must do instead is **assert the invariant rather than assume it**.
If `enqueue` returns `None` here, something upstream changed — a `dedup_key`
added to this call site, or the index's partial filter altered — and the correct
response is to fail loudly at the moment of the change, naming it, rather than
to fan out a bucket set that is silently short one bucket. `PermanentError`, not
`RetryableError`: no number of retries fixes a call site that now dedups.

## Decision Q3: the bare `except Exception: continue` goes

`:84-87` is the reason a two-line type error cost 24 hours. It cannot
distinguish "this id is malformed" (a bug, immediately fatal, never
self-correcting) from "Mongo hiccuped" (transient, worth another poll), and it
resolves both to "skip this bucket forever."

The sub-job ids are validated once, at fan-out, where the value is created.
`PydanticObjectId(str(job.id))` either works there or the fan-out fails — which
is the right place for it, because a malformed id is a defect in the code that
produced it, not in the code that reads it.

In the polling loop, `Job.get` on an already-validated id has one remaining
failure mode: the database is unavailable. That is genuinely transient and worth
another poll — but it must be logged as what it is and it must not be able to
mask a lookup failure indefinitely. So: catch the narrow database error, log,
and continue; let anything else propagate. A `ValidationError` reaching the poll
loop after this change means the fan-out validation was bypassed, and it should
crash the job with a traceback rather than be absorbed.

## Decision Q4: `RetryableError` with `max_attempts=1` is a permanent failure wearing the wrong name

Traced through `backend/app/queue/executor.py:222-241`. `RetryableError` and
bare `Exception` share one handler; it computes `attempts = job.attempts + 1`
and branches:

```python
if attempts >= job.max_attempts:
    outcome = RunOutcome.DEAD
    await queue.complete(job_id, epoch, state=JobState.DEAD, error=error)
```

With `max_attempts=1` (`chunked_align_handlers.py:32`), a first attempt gives
`attempts = 1 >= 1`, so the job goes straight to `DEAD`. **It never retries.**
Both `RetryableError` raises in this handler — the failed-bucket one at `:96-99`
and the timeout at `:113-116` — are permanent failures.

They are also *correctly* permanent, which is the point. Re-running the
orchestrator would re-enqueue every bucket from scratch, discarding buckets that
already succeeded — the exact waste `max_attempts=1` exists to prevent. The
error type is what is wrong, not the retry count.

The user-visible cost is real: the error is stored with `"retryable": True`
(`executor.py:229`) on a job in state `DEAD`. Anything that reads that pair —
a person, a support conversation, a UI branch — is told the job can be retried
when the queue has already decided it cannot. Both raises become
`PermanentError`, which `executor.py:205-219` handles with the comment "Cannot
succeed however many times we try, so do not burn retries" and records
`"retryable": False`.

**This is not merely cosmetic and belongs in this fix**, because R-851-4's
timeout path is the exact symptom of the bug being fixed, and leaving it
mislabelled means the one artefact a user would have to diagnose the next
occurrence lies about the job's disposition.

## Requirements

Permanent identifiers. Never reused.

- **R-851-1.** `align_reads_chunked` collects each sub-job's id as a string that
  `PydanticObjectId(...)` accepts.
- **R-851-2.** `align_reads_chunked` fails immediately with a permanent error
  when `queue.enqueue` returns `None` for a bucket, naming deduplication as the
  cause.
- **R-851-3.** The sub-job polling loop does not swallow a malformed-id error
  into a skipped bucket.
- **R-851-4.** `align_reads_chunked` reports a bucket failure and a deadline
  expiry as permanent errors, not retryable ones.
- **R-851-5.** `_apply_align_reads_chunked` resolves every id
  `align_reads_chunked` produced.
- **R-851-6.** The orchestrator's test fixture returns what `queue.enqueue`
  returns, not what the handler assumes it returns.

## Testing

- **R-851-6 first, and it must fail before the fix.** Repoint
  `test_chunked_align_orchestrator.py:64-72`'s `_enqueue` to return a `Job`-like
  object with an `.id`, matching the real signature. Run the existing suite
  against the *unfixed* handler and confirm it goes red. A fixture change that
  leaves the suite green means it is still not exercising the real contract, and
  the rest of these tests are worthless.
- **R-851-1** — assert `result["sub_job_ids"]` are strings, and that
  `PydanticObjectId(x)` succeeds on each. Asserting the type alone passes for a
  `str(<Job object>)`.
- **R-851-2** — `_enqueue` returns `None` for one bucket; assert
  `PermanentError` with "dedup" in the message. Q2 establishes this is
  unreachable in production; the test guards the invariant, and its docstring
  should say so and name what would make it reachable (a `dedup_key` added to
  this call site).
- **R-851-3** — `Job.get` raises a `ValidationError`; assert it propagates
  rather than being absorbed into a skipped bucket.
- **R-851-4** — assert `PermanentError` on both the failed-bucket path and the
  deadline path, replacing the two existing `RetryableError` assertions at
  `test_chunked_align_orchestrator.py:154` and `:158`.
- **R-851-5** — the end-to-end shape: feed `_apply_align_reads_chunked` the ids
  the orchestrator actually produced and assert it resolves all of them and
  enqueues `merge_chunked_buckets`. `test_chunked_align_merge.py:48-52` already
  builds this result dict via `str(j)`, which is what the fix makes true; that
  file needs no change and confirming it needs none is part of the work.
- **Real-data check** — run one chunked alignment end to end against a real
  reference and confirm it merges. This bug means no chunked alignment has ever
  completed, so nothing short of a real run establishes that the path works.

## Verify before implementing

1. **Whether `enqueue` can return a `Job` in a non-`PENDING` state here.** The
   `depends_on` branch (`queue.py:157-161`) returns the job early after
   `_handle_dependencies` may have failed or blocked it. `chunked_align_handlers`
   passes no `depends_on`, so the branch is not taken — confirm by reading, and
   if it can be reached, the fan-out must check the returned job's state too.
2. **Whether any chunked alignment has ever succeeded in this deployment.**
   Query `job_timings` for `align_reads_chunked` via `timing_service` (not the
   collection directly, per CLAUDE.md). A success would contradict Q1 and the
   diagnosis needs revisiting before the fix lands.
3. **The narrow database exception to catch in the poll loop** (Q3) — what Motor
   or Beanie actually raises on an unavailable database, so the replacement
   `except` names a real class rather than a guessed one.

## Out of scope

- **`merge_chunked_buckets` full-sorting the merged BAM.** #835's second noted
  defect; buckets are disjoint by reference sequence and each already sorted, so
  a header-order-preserving concatenation may avoid the re-sort. Worth measuring
  before assuming it matters. Separate ticket.
- **Node-aware placement of sub-jobs.** #843 and #845.
- **The bucket-packing policy** (`pack_buckets` is first-fit-decreasing against
  a memory budget, not per-sequence). #835's first gap.
- **The 24h deadline's duration.** Q4 corrects its error *type*; whether 86400
  is the right number is a separate question and, with the bug fixed, one with
  no evidence behind it yet.
- **The bare-`except` pattern elsewhere in the queue.** This fix corrects
  `chunked_align_handlers.py:84-87`. A sweep is its own ticket.
