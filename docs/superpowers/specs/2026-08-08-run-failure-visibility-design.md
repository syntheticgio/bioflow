# Surfacing run failures in the Recent runs ledger

Design for [#95](https://github.com/syntheticgio/bioflow/issues/95), 2026-08-08.

## The problem

A failed run in the Activity view's "Recent runs" ledger reports `FAILED · 2
JOBS` and offers no way to find out why. Expanding the row shows the run's
parameters and links back to its input files. Nothing on the page says what
went wrong.

The cause is a grouping decision made for a different reason.
`ActivityView` builds a set of every job that belongs to a run and filters
those out of the loose-job list below the columns
([ActivityView.tsx:104-111](../../../frontend/src/components/ActivityView.tsx)),
so that an alignment's seven jobs appear once as a run rather than seven times
as loose rows. But that loose list is the *only* place on the page that
renders `job.error`, the "Explain this error" button, and `JobLogView`
([ActivityView.tsx:413-420](../../../frontend/src/components/ActivityView.tsx)).
Grouping a job into a run therefore removes the only path to its diagnosis.
The tidier the page got, the less diagnosable it became.

Both of the issue's questions already have answers in the codebase; neither is
reachable from this surface. There is a per-job log (`JobLogView`, backed by
`GET /jobs/{id}/log`), and there is a cached plain-language error explainer
(`failure_explanation_service`, backed by
`GET /pipelines/failure-explanation`).

## No backend change is needed

`run_service.status_for` already returns each member job's `error`, `state`,
`type`, and `job_id`
([run_service.py:223-236](../../../backend/app/services/run_service.py)), and
that shape is typed all the way through to the frontend as `RunMemberJob`
([types.ts:678](../../../frontend/src/api/types.ts)), including
`error: { code, message, retryable } | null`.

`ActivityView` already fetches every visible run's detail — it needs them for
the `jobCounts` map it passes to the ledger. So the error text for the failed
run in the issue screenshot was in the browser's memory when the screenshot was
taken. `RunLedger` receives only `jobCounts: Map<string, number>` and discards
the rest.

This is a display fix. The API, the services, and the models are untouched.

## Change 1 — extract `FailureExplanationExpander`

`FailureExplanationExpander` currently lives inside `ActivityView.tsx`
(lines 431-479), declared next to `JobRow`. It moves verbatim to
`frontend/src/components/activity/FailureExplanationExpander.tsx` and is
exported; `ActivityView` imports it rather than declaring it.

It is already a pure `(code, message)` component with no coupling to
`ActivityView`'s state, so this is a move rather than a rewrite. The reason to
move it is direction of dependency: `activity/RunLedger.tsx` importing from the
parent module that renders it would be backwards, and the two files would be
circularly coupled.

Its behaviour is preserved exactly, including the property the comment above it
calls out — click-triggered only, never generated automatically on failure, and
an unconfigured or unproductive model means the button disappears rather than
replacing or hiding the raw error text.

## Change 2 — `RunLedger` takes jobs, not just a count

`RunLedger`'s `jobCounts: Map<string, number>` becomes
`jobsByRun: Map<string, RunMemberJob[]>`. `LedgerRow`'s `jobs?: number` becomes
`jobs?: RunMemberJob[]`, and the meta line derives its count from
`jobs?.length` — the rendered "2 jobs" text is unchanged.

`ActivityView` passes `details.get(r.id)?.jobs ?? []` where it passed
`details.get(r.id)?.jobs.length ?? 0`. Same source, nothing extra fetched.

`ActivityLead` also renders `LedgerRow`, for the second and subsequent
in-progress runs. It gets the same prop change so the two call sites stay in
step.

## Change 3 — the failure block

Rendered inside the existing `ledger-detail` expansion, below the facts grid
and the input links, and only when `run.status === "failed"`. Clicking the row
opens what it opens today plus this block; the row keeps its single click
contract, and the error sits next to the parameters — which is often the
context that explains it, since a wrong preset for PacBio reads is a parameters
question as much as an error question.

The block has three parts.

**A shape line**, e.g. `6 of 7 jobs succeeded` — the count of members with
`state === "succeeded"` against the total. A run that died at the last step
after doing all the work and a run that died immediately are different
diagnoses, and the run detail already carries the states needed to tell them
apart. One line of text rather than a list of rows nobody needs.

**One entry per member job with a non-null `error`**, each carrying:

- `code: message`, styled with `var(--error)` as `JobRow` does;
- a `FailureExplanationExpander` for that code and message;
- a log toggle rendering `<JobLogView jobId={job.job_id} live={false} />`.
  `live` is false because a run in this ledger is finished by definition.
  `JobLogView` needs nothing but an id, and already treats both "no log" and
  "empty log" as normal answers — which matters, because only jobs that shell
  out to an external tool write one.

The log is the substantive half of this change. A failed aligner's real
diagnosis is almost always in the tool's stderr rather than in the one-line
`code: message` above it.

**A fallback line when no member carries an error**:

> No job reported an error; the run may have been cancelled or a job may have
> expired.

This case is real and is why the block cannot simply be a list. A job cancelled
because a sibling failed has no `error` set, and a job pruned by the 30-day TTL
comes back with `type` and `state` null. Without the fallback, such a run would
show `FAILED`, expand, and render an empty block — which reads as broken rather
than as merely unhelpful, and would be worse than the current behaviour.

Log open/closed state is local to the row, one open at a time, mirroring the
single-open-row rule `RunLedger` already applies to the rows themselves.

## Styling

New rules alongside the existing `.ledger-detail` block in
`frontend/src/styles/broadsheet.css` (around line 1425). `.job-log` is already
styled from its use in the loose-job list and needs nothing new.

## Testing

This repo has no headless component-testing setup and none is expected — there
are zero `.test.tsx` files. Verification is manual, in the browser. From this
worktree that means `./ops/worktree-up.sh` and localhost:5273, against the real
failed run from the issue rather than a fabricated one.

Four cases to walk:

1. A failed run with one failed member job — error text, explanation button,
   and log all reachable.
2. A succeeded run — the block is absent entirely, and the row behaves as it
   does today.
3. A failed run whose members carry no `error` — the fallback line renders.
4. A failed job that wrote no log — `JobLogView` says so rather than hanging or
   erroring.

The backend is untouched, so `./backend/run-worktree-tests.sh tests/ -q` is a
regression check rather than the point of the exercise.

## Out of scope

- **Retrying a run from the ledger.** The issue asks to see the errors.
- **Changing when explanations are generated.** The opt-in-by-click behaviour is
  deliberate and is preserved.
- **Any backend change.** The data is already served and already in the browser.
