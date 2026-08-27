# Chunked sub-job ids — implementation plan

Issue: [#851](https://github.com/syntheticgio/bioflow/issues/851)
Design: `docs/superpowers/specs/2026-08-25-chunked-subjob-id-design.md`

Status: **SHIPPED** in `55ad31e2` ("fix(queue): collect chunked sub-job ids as
ids, not Job objects"); #851 is closed. Retained as the design record for that
fix. No dependencies. Small diff, but it was the difference between chunked
alignment never working and working.

## Start here: make the test suite able to fail

Do this before editing the handler. The existing suite is green against a broken
handler because its fixture returns a string id where the real `enqueue` returns
a `Job` (`backend/tests/queue/test_chunked_align_orchestrator.py:64-72`).

Change `_enqueue` to return a `Job`-like object:

```python
async def _enqueue(job_type, *, payload=None, owner=None, parent_job_id=None, **kw):
    jid = PydanticObjectId()
    enqueued.append({..., "id": str(jid)})
    return SimpleNamespace(id=jid)
```

Then run the suite against the **unfixed** handler:

```bash
./backend/run-worktree-tests.sh tests/queue/test_chunked_align_orchestrator.py -q
```

`TestWaitingForBuckets` must go red. If it stays green, the fixture is still not
exercising the real contract and nothing below is verified by anything. Do not
proceed past a green run here.

## Changes

### 1. `backend/app/queue/chunked_align_handlers.py:56-61` — collect ids

```python
job = await queue.enqueue(
    "align_reads",
    payload=sub_payload,
    owner=owner,
    parent_job_id=ctx.job_id,
)
if job is None:
    raise PermanentError(
        f"Bucket {spec['index']} of {total} was deduplicated away rather than "
        "enqueued, so this alignment would merge an incomplete reference. "
        "This call site passes no dedup_key and cannot normally dedup -- if "
        "this fires, one was added, or the unique index on Job.dedup_key "
        "changed."
    )
sub_job_ids.append(str(job.id))
```

`PermanentError`, not `RetryableError` — per the spec's Q2, no retry fixes a
call site that now dedups. `PermanentError` is already imported (`:11`).

`str(job.id)` is what makes `_apply_align_reads_chunked`'s
`PydanticObjectId(jid)` (`results.py:1465`) work. The list is already annotated
`list[str]` at `:47`; it was simply never true.

The message is long on purpose. Q2 establishes this branch is unreachable today,
so whoever sees it fire is looking at a changed invariant and needs to be told
which one.

### 2. `backend/app/queue/chunked_align_handlers.py:84-87` — narrow the except

```python
for jid in sub_job_ids:
    job = await Job.get(PydanticObjectId(jid))
```

with the bare `except Exception: continue` replaced by a catch of only the
transient database error. **Confirm the class before writing it** (spec's Verify
item 3) — do not guess at `PyMongoError` vs a Motor-specific class. Log it as
what it is, then `continue`.

Anything else propagates and fails the job with a traceback, which is the
behaviour change that matters: a `ValidationError` here after change 1 means the
fan-out validation was bypassed, and it should crash loudly rather than be
absorbed into a 24-hour poll.

`PydanticObjectId` needs importing (`from beanie import PydanticObjectId`); the
module does not currently import it.

**Same edit:** `:78`'s `request_cancel(cid)` iterates the same list. Check its
signature — if it takes an id string, change 1 makes it correct for the first
time; if it takes something else, it needs the same treatment. Grep
`queue.py` for `async def request_cancel`.

### 3. `backend/app/queue/chunked_align_handlers.py:96-99` and `:113-116` — permanent, not retryable

Both `RetryableError` raises become `PermanentError`. Per the spec's Q4, with
`max_attempts=1` (`:32`) `executor.py:233` already sends these to `DEAD` on the
first attempt — the change is that the stored error stops claiming
`"retryable": True` (`executor.py:229`) about a job the queue has already
decided will never run again.

Keep the message text identical; the tests match on it and it is good text.

**Same commit:** the `RetryableError` import at `:11` — check whether it is
still used in this module. `merge_chunked_buckets` raises it at three points
(the merge, sort and their exit codes), and that handler has `max_attempts=2`,
where it is correct. So the import stays. Verify rather than assume; ruff's
`F401` will catch it if it does not, which is one of the reasons to run ruff
locally (it is not a CI check).

### 4. `backend/tests/queue/test_chunked_align_orchestrator.py` — the tests

Beyond the fixture change above:

- `:154` and `:158` — `pytest.raises(RetryableError, ...)` becomes
  `PermanentError` in `test_one_failed_bucket_fails_the_whole_alignment` and
  `test_a_cancelled_bucket_fails_the_whole_alignment`. The import at `:22` must
  change with them or the file fails at collection.
- `test_all_succeeded_returns_the_merge_inputs` (`:136-138`) already asserts
  `result["sub_job_ids"] == [e["id"] for e in enqueued]`, and with `e["id"]`
  now `str(jid)` this becomes the R-851-1 assertion. Strengthen it with an
  explicit `PydanticObjectId(x)` round-trip on each — asserting `isinstance(x,
  str)` alone passes for `str(<Job object>)`.
- **New:** a dedup test. `_enqueue` returns `None` for the second bucket; assert
  `PermanentError` matching "dedup". Docstring per the spec: this branch is
  unreachable in production because the call site passes no `dedup_key` and the
  unique index's partial filter (`models/job.py:235-243`) exempts a `None` key —
  the test guards the invariant, and names what would break it.
- **New:** a malformed-id test. `Job.get` raises a `ValidationError`; assert it
  propagates rather than producing a bucket that is never counted. This is the
  regression test for the actual bug and it must not be omitted because change 1
  "makes it impossible."

The module docstring (`:1-13`) explains what these tests are for and predates
the bug being known. Add a paragraph: the fixture returned a string id where
`enqueue` returns a `Job`, which is why a suite explicitly written to catch a
bucket going missing at the *start* did not catch every bucket going missing at
every poll.

### 5. `backend/tests/queue/test_chunked_align_merge.py` — confirm, do not change

`:48-52` builds the orchestrator result with `"sub_job_ids": [str(j) for j in
sub_job_ids]`, which is exactly what change 1 makes real. Read it, confirm no
edit is needed, and say so in the PR — a reviewer will reasonably ask whether
the applier's tests were also mocking the wrong contract. They were not.

## Commits

1. `test(queue): make the chunked orchestrator's fixture return what enqueue returns`
   — the fixture change alone, committed red. Body: the suite could not fail
   because the fixture returned a string id where `enqueue` returns `Job | None`.
   Committing this separately is what makes the next commit demonstrably a fix
   rather than a claim.
2. `fix(queue): collect chunked sub-job ids as ids, not Job objects` — changes
   1, 2, and the tests from change 4 that cover them. The changelog entry.
   Body must say what was actually broken: every bucket of every chunked
   alignment failed its lookup on every poll, was never counted, and the
   orchestrator spun to its 24h deadline — so no chunked alignment has ever
   completed. Name the two things that hid it (the bare `except`, the fixture).
3. `fix(queue): report chunked alignment failures as permanent, not retryable`
   — change 3 and its two test updates. Separable: it is a correctness fix to
   the error *type* that stands on its own, and `max_attempts=1` already made
   these terminal, so reverting it does not resurrect the bug.

`fix`, not `chore` — both reach users and therefore the changelog. Commit 1 is
`test` and is filtered out of user-facing notes, which is correct.

## PR

Title: `fix(queue): chunked alignment treats enqueue's return as a job id` —
the issue's own title, commit-subject standard, lands in the release notes
verbatim. Labels `type:bug` + `area:backend` + `area:pipelines` so
`.github/release.yml` files it correctly. `Closes #851`.

The description must carry what the diff cannot:

- **The bug is total, not conditional.** The ticket describes the normal case as
  incidentally working. It does not work: `Job.get(<Job>)` raises a
  `ValidationError` through `parse_object_as`, verified empirically in the `api`
  container. Every bucket, every poll.
- **Why nobody noticed.** The bare `except` made a type error look like a
  transient lookup miss, and the test fixture returned the value the code
  assumed rather than the one the queue returns.
- **The dedup branch is unreachable** and why (no `dedup_key` at the call site;
  `queue.py:141-146`; the `$type: "string"` partial filter at
  `models/job.py:235-243`). It is guarded as an invariant, not handled as a case.

## Verification

- `./backend/run-worktree-tests.sh tests/queue/test_chunked_align_orchestrator.py tests/queue/test_chunked_align_merge.py -q`
  — from a worktree; `docker compose exec api` silently tests main's code.
- `./backend/run-worktree-tests.sh tests/queue/ -q` — the whole queue suite.
  Change 3 alters an error type that `executor.py` branches on.
- `ruff check --config backend/pyproject.toml backend/app backend/tests ops e2e`
  from the repo root. Catches the `F401` if change 3's import analysis was wrong.
- **`docker compose restart worker` before any manual check.** `worker` does not
  hot-reload, and `chunked_align_handlers.py` is a queue handler — without the
  restart the job runs the old in-memory code while appearing to run the fix.
- **Real run, and it is not optional.** Launch one chunked alignment through the
  UI against a real reference and confirm it merges and produces an alignment
  object. Per the spec, this path has never completed, so the unit tests
  establish that the ids are right and nothing else. Confirm the run finishes in
  minutes rather than sitting at "Buckets 0/N".
- Query `job_timings` through `timing_service` (never the collection directly)
  to confirm no historical `align_reads_chunked` success contradicts the
  diagnosis — spec Verify item 2, worth doing before the PR rather than after.

## Out of scope

Per the spec: the merge's redundant full sort, node-aware placement, the
bucket-packing policy, the 24h deadline's duration, and a sweep of bare-`except`
patterns elsewhere in the queue. Any of these that looks worth doing gets a
filed issue, not a wider diff.
