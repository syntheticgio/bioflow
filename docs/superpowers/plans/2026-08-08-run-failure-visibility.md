# Run Failure Visibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a failed run in the Activity view's "Recent runs" ledger show its error, an AI explanation, and the failing job's tool log, by rendering data the browser already has.

**Architecture:** Display-only change in three parts. Extract `FailureExplanationExpander` from `ActivityView.tsx` into its own module so the ledger can import it without a backwards dependency. Change `RunLedger`'s `jobCounts: Map<string, number>` prop into `jobsByRun: Map<string, RunMemberJob[]>` so the row keeps the member jobs instead of discarding them. Add a `RunFailureBlock` component, rendered inside the existing `ledger-detail` expansion when the run failed.

**Tech Stack:** React 18 + TypeScript, TanStack Query (already wired), plain CSS in `frontend/src/styles/broadsheet.css`. No backend change.

**Spec:** [`docs/superpowers/specs/2026-08-08-run-failure-visibility-design.md`](../specs/2026-08-08-run-failure-visibility-design.md)

---

## Read this before Task 1

**There is no test framework for this change.** This repo has zero `.test.tsx`
files, no jsdom, and no testing-library. That is deliberate and documented in
`CLAUDE.md`: manual testing in the browser is the actual verification step for
anything UI-facing. Do not add a component test framework as part of this plan
— that would be a far larger and more invasive change than the fix, and it is
not what was asked for.

The TDD loop is therefore replaced by two real gates you **must** run:

1. `npx tsc --noEmit` from `frontend/` — the type checker is the mechanical
   safety net here, and the prop change in Task 2 is specifically designed so
   that a missed call site is a **compile error** rather than a runtime
   surprise. Task 2 deliberately begins with a step where this command *fails*,
   which is that task's equivalent of a red test.
2. Manual browser verification against a real failed run (Task 6).

**Do not run bare `docker compose` from this worktree.** A `PreToolUse` hook
blocks it, and the reason is in `CLAUDE.md`: relative bind mounts would silently
repoint the main stack on port 5173 at this worktree. Use
`./ops/worktree-up.sh`, which brings up a separate stack on ports 5273/8100.

**Backend is untouched by every task in this plan.** If you find yourself
editing anything under `backend/`, stop — you have misread the plan.

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `frontend/src/components/activity/FailureExplanationExpander.tsx` | Create | The "Explain this error" button and its fetched result. Moved verbatim out of `ActivityView.tsx`. |
| `frontend/src/components/activity/RunFailureBlock.tsx` | Create | Given a run's member jobs, render the shape line, each errored job with its explainer and log toggle, or the fallback line. |
| `frontend/src/components/ActivityView.tsx` | Modify | Delete the local `FailureExplanationExpander`, import it instead; pass `jobsByRun` to `RunLedger`. |
| `frontend/src/components/activity/RunLedger.tsx` | Modify | Take `RunMemberJob[]` instead of a count; render `RunFailureBlock` in the detail area. |
| `frontend/src/components/activity/ActivityLead.tsx` | Modify | Pass the jobs array to `LedgerRow` to match its new prop type. |
| `frontend/src/styles/broadsheet.css` | Modify | Style the failure block, next to the existing `.ledger-detail` rules. |

`RunFailureBlock` is its own file rather than living inside `RunLedger.tsx`
because it is the only genuinely new logic in this change, it has one clear
responsibility, and keeping it separate leaves `RunLedger.tsx` recognisably the
file it is today.

---

### Task 1: Extract `FailureExplanationExpander` into its own module

This is a pure move. The component's behaviour must not change: it is
click-triggered only, and when the API returns null it renders nothing rather
than replacing the raw error text.

**Files:**
- Create: `frontend/src/components/activity/FailureExplanationExpander.tsx`
- Modify: `frontend/src/components/ActivityView.tsx` (delete lines 425-479, adjust imports)

- [ ] **Step 1: Create the new module**

Create `frontend/src/components/activity/FailureExplanationExpander.tsx` with
exactly this content. The body is copied from `ActivityView.tsx`; only the
`export` keyword and the import lines are new.

```tsx
import { useState } from "react";
import { api } from "../../api/client";

/**
 * "Explain this error" -- click-triggered only, never generated
 * automatically on job failure. A model that is not configured or that
 * produces nothing means the button simply does not appear; the raw
 * error text above it is never replaced or hidden.
 *
 * Lives here rather than in ActivityView so that both the loose-job list
 * and the run ledger can use it. The ledger is a child of ActivityView, so
 * importing it from there would be a backwards dependency.
 */
export function FailureExplanationExpander({
  code,
  message,
}: {
  code: string;
  message: string;
}) {
  const [state, setState] = useState<
    | { status: "idle" }
    | { status: "loading" }
    | { status: "unavailable" }
    | { status: "shown"; text: string; model: string | null }
  >({ status: "idle" });

  if (state.status === "unavailable") return null;

  if (state.status === "shown") {
    return (
      <div style={{ marginTop: 4, color: "var(--text-faint)" }}>
        {state.text}
        {state.model && (
          <span style={{ color: "var(--text-faint)" }}> (AI-generated, {state.model})</span>
        )}
      </div>
    );
  }

  return (
    <button
      type="button"
      className="btn-text"
      style={{ marginLeft: 8 }}
      disabled={state.status === "loading"}
      onClick={async () => {
        setState({ status: "loading" });
        try {
          const result = await api.failureExplanation(code, message);
          if (result == null) {
            setState({ status: "unavailable" });
          } else {
            setState({ status: "shown", text: result.text, model: result.model });
          }
        } catch {
          setState({ status: "unavailable" });
        }
      }}
    >
      {state.status === "loading" ? "Explaining…" : "Explain this error"}
    </button>
  );
}
```

- [ ] **Step 2: Delete the original from `ActivityView.tsx`**

In `frontend/src/components/ActivityView.tsx`, delete the entire block starting
with this comment (currently line 425) through the closing brace of the
function (currently line 479):

```tsx
/**
 * "Explain this error" -- click-triggered only, never generated
 * automatically on job failure. A model that is not configured or that
 * produces nothing means the button simply does not appear; the raw
 * error text above it is never replaced or hidden.
 */
function FailureExplanationExpander({
```

Delete through the end of that function. The call site at line 416 stays as it
is — it will resolve to the import added in the next step.

- [ ] **Step 3: Add the import to `ActivityView.tsx`**

In the import block at the top of `frontend/src/components/ActivityView.tsx`,
add this line after the existing `ActivityLead` import (line 10):

```tsx
import { FailureExplanationExpander } from "./activity/FailureExplanationExpander";
```

- [ ] **Step 4: Type-check**

Run from the `frontend/` directory:

```bash
npx tsc --noEmit
```

Expected: no output, exit code 0. If it reports `Cannot find name
'FailureExplanationExpander'`, Step 3's import is missing or misspelled. If it
reports the symbol is declared but never used, Step 2 deleted the call site
instead of the definition — restore line 416.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/activity/FailureExplanationExpander.tsx frontend/src/components/ActivityView.tsx
git commit -m "refactor(activity): extract FailureExplanationExpander into its own module"
```

---

### Task 2: Give `LedgerRow` the member jobs instead of a count

The prop change is the point of this task, and it is deliberately a **breaking**
type change: every call site must be updated, and `tsc` names the ones that
weren't. Step 2 runs the type checker expecting failure — that is this task's
red test.

**Files:**
- Modify: `frontend/src/components/activity/RunLedger.tsx:15-53` and `:62-84`
- Modify: `frontend/src/components/ActivityView.tsx:159-168`
- Modify: `frontend/src/components/activity/ActivityLead.tsx:63-71`

- [ ] **Step 1: Change the prop types in `RunLedger.tsx`**

In `frontend/src/components/activity/RunLedger.tsx`, add `RunMemberJob` to the
existing type import on line 2:

```tsx
import type { RunMemberJob, RunSummary } from "../../api/types";
```

Change the `RunLedger` signature (lines 15-24). Replace:

```tsx
export function RunLedger({
  runs,
  jobCounts,
  onSelect,
}: {
  runs: RunSummary[];
  /** Job count per run id, from the details already fetched by the page. */
  jobCounts: Map<string, number>;
  onSelect: (objectId: string, projectId: string) => void;
}) {
```

with:

```tsx
export function RunLedger({
  runs,
  jobsByRun,
  onSelect,
}: {
  runs: RunSummary[];
  /** Member jobs per run id, from the details already fetched by the page.
   *  The count comes from this too -- the array is what a failed row needs to
   *  say anything about why it failed. */
  jobsByRun: Map<string, RunMemberJob[]>;
  onSelect: (objectId: string, projectId: string) => void;
}) {
```

Then update the `LedgerRow` call inside it (line 42). Replace:

```tsx
              jobs={jobCounts.get(run.id)}
```

with:

```tsx
              jobs={jobsByRun.get(run.id)}
```

- [ ] **Step 2: Change `LedgerRow`'s prop type and count derivation**

Still in `RunLedger.tsx`, in the `LedgerRow` props (line 71), replace:

```tsx
  jobs?: number;
```

with:

```tsx
  jobs?: RunMemberJob[];
```

Then fix the meta line that used it as a number (lines 78-84). Replace:

```tsx
  const facts = runFacts(run);
  const meta = [
    STATUS_LABELS[run.status],
    jobs != null ? `${jobs} ${jobs === 1 ? "job" : "jobs"}` : null,
    formatClock(run.updated_at),
  ]
    .filter(Boolean)
    .join(" · ");
```

with:

```tsx
  const facts = runFacts(run);
  const jobCount = jobs?.length;
  const meta = [
    STATUS_LABELS[run.status],
    jobCount != null ? `${jobCount} ${jobCount === 1 ? "job" : "jobs"}` : null,
    formatClock(run.updated_at),
  ]
    .filter(Boolean)
    .join(" · ");
```

The rendered text is unchanged: a run with two jobs still reads "2 jobs".

- [ ] **Step 3: Run the type checker and confirm it fails**

Run from the `frontend/` directory:

```bash
npx tsc --noEmit
```

Expected: FAIL, with two errors naming the two call sites that still pass a
number — one in `ActivityView.tsx` (the `jobCounts` prop no longer exists) and
one in `ActivityLead.tsx` (`number` is not assignable to `RunMemberJob[]`).

If it passes, the prop rename in Step 1 did not take effect — check that you
renamed `jobCounts` and not just its type.

- [ ] **Step 4: Update the `ActivityView.tsx` call site**

In `frontend/src/components/ActivityView.tsx` (lines 159-168), replace:

```tsx
        <RunLedger
          runs={finishedRuns.slice(0, LEDGER_LIMIT)}
          jobCounts={
            new Map(
              finishedRuns.map((r) => [r.id, details.get(r.id)?.jobs.length ?? 0]),
            )
          }
          onSelect={selectObject}
        />
```

with:

```tsx
        <RunLedger
          runs={finishedRuns.slice(0, LEDGER_LIMIT)}
          jobsByRun={
            new Map(
              finishedRuns.map((r) => [r.id, details.get(r.id)?.jobs ?? []]),
            )
          }
          onSelect={selectObject}
        />
```

- [ ] **Step 5: Update the `ActivityLead.tsx` call site**

In `frontend/src/components/activity/ActivityLead.tsx` (line 67), replace:

```tsx
          jobs={details.get(run.id)?.jobs.length}
```

with:

```tsx
          jobs={details.get(run.id)?.jobs}
```

No import change is needed — `ActivityLead.tsx` already imports `RunMemberJob`
on line 4.

- [ ] **Step 6: Run the type checker and confirm it passes**

Run from the `frontend/` directory:

```bash
npx tsc --noEmit
```

Expected: no output, exit code 0.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/activity/RunLedger.tsx frontend/src/components/ActivityView.tsx frontend/src/components/activity/ActivityLead.tsx
git commit -m "refactor(activity): pass member jobs to LedgerRow instead of a count"
```

---

### Task 3: Build the `RunFailureBlock` component

This is the new logic. Three cases, and the third is the one that is easy to
forget: a run can be `failed` with **no** member carrying an `error` — when a
job was cancelled because a sibling failed, or when jobs aged past the 30-day
TTL and come back with `state: null`. That case must produce a sentence, not an
empty div.

**Files:**
- Create: `frontend/src/components/activity/RunFailureBlock.tsx`

- [ ] **Step 1: Create the component**

Create `frontend/src/components/activity/RunFailureBlock.tsx` with exactly this
content:

```tsx
import { useState } from "react";
import type { RunMemberJob } from "../../api/types";
import { ROLE_LABELS } from "../../lib/runFormat";
import { JobLogView } from "../JobLogView";
import { FailureExplanationExpander } from "./FailureExplanationExpander";

/**
 * Why a run failed, inside the ledger row's existing expansion.
 *
 * The data is already on the page -- `run_service.status_for` returns each
 * member's error, and ActivityView fetches every run's detail for its job
 * counts. Until this existed, the ledger threw it away, and a job that
 * belonged to a run was the one kind of job whose error and log were
 * unreachable: the loose-job list below the columns renders both, but
 * deliberately excludes anything grouped into a run.
 */
export function RunFailureBlock({ jobs }: { jobs: RunMemberJob[] }) {
  // One log open at a time, matching the ledger's own single-open-row rule.
  const [openLog, setOpenLog] = useState<string | null>(null);

  const failed = jobs.filter((j) => j.error != null);
  const succeeded = jobs.filter((j) => j.state === "succeeded").length;

  return (
    <div className="ledger-failure">
      {/* Where in the run it died. Failing at the last step after doing all
          the work and failing at the first are different diagnoses, and this
          is one line rather than a list of rows nobody needs. */}
      {jobs.length > 0 && (
        <div className="ledger-failure-shape">
          {succeeded} of {jobs.length} {jobs.length === 1 ? "job" : "jobs"} succeeded
        </div>
      )}

      {failed.length === 0 ? (
        // A cancelled sibling leaves no error, and a pruned job comes back
        // with null state. Without this line the block would render empty,
        // which reads as broken rather than as merely unhelpful.
        <div className="ledger-failure-none">
          No job reported an error; the run may have been cancelled or a job may
          have expired.
        </div>
      ) : (
        failed.map((job) => (
          <div key={job.job_id} className="ledger-failure-job">
            <div className="ledger-failure-head">
              <span className="ledger-failure-role">
                {ROLE_LABELS[job.role] ?? job.role}
              </span>
              <button
                type="button"
                className="btn-text"
                onClick={() =>
                  setOpenLog((o) => (o === job.job_id ? null : job.job_id))
                }
              >
                {openLog === job.job_id ? "Hide log" : "Show log"}
              </button>
            </div>

            <div className="ledger-failure-error">
              {job.error!.code}: {job.error!.message}
              <FailureExplanationExpander
                code={job.error!.code}
                message={job.error!.message}
              />
            </div>

            {/* Never live: a run in this ledger has finished by definition. */}
            {openLog === job.job_id && (
              <JobLogView jobId={job.job_id} live={false} />
            )}
          </div>
        ))
      )}
    </div>
  );
}
```

The `job.error!` non-null assertions are safe because `failed` is filtered on
`j.error != null`; TypeScript does not narrow through `Array.prototype.filter`
without a type predicate, and adding one here would be noise.

- [ ] **Step 2: Type-check**

Run from the `frontend/` directory:

```bash
npx tsc --noEmit
```

Expected: no output, exit code 0. The component is not rendered anywhere yet —
that is Task 4 — so this only confirms it compiles.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/activity/RunFailureBlock.tsx
git commit -m "feat(activity): add RunFailureBlock for a failed run's errors and logs"
```

---

### Task 4: Render the block in the ledger row

**Files:**
- Modify: `frontend/src/components/activity/RunLedger.tsx` (imports, and the `ledger-detail` area)

- [ ] **Step 1: Import the block**

In `frontend/src/components/activity/RunLedger.tsx`, add after the existing
`SectionHead` import (line 5):

```tsx
import { RunFailureBlock } from "./RunFailureBlock";
```

- [ ] **Step 2: Render it inside the expansion**

In `LedgerRow`, inside the `{open && (<div className="ledger-detail">...)}`
block, add the failure block **after** the `ledger-links` section — so the
error sits below the parameters and the input links, at the end of the
expansion. Replace this (currently lines 119-132):

```tsx
          {run.inputs.length > 0 && (
            <div className="ledger-links">
              {run.inputs.map((i) => (
                <button
                  key={`${i.object_id}-${i.role}`}
                  type="button"
                  className="run-input-link"
                  onClick={() => onSelect(i.object_id, run.project_id)}
                >
                  {i.name}
                </button>
              ))}
            </div>
          )}
```

with:

```tsx
          {run.inputs.length > 0 && (
            <div className="ledger-links">
              {run.inputs.map((i) => (
                <button
                  key={`${i.object_id}-${i.role}`}
                  type="button"
                  className="run-input-link"
                  onClick={() => onSelect(i.object_id, run.project_id)}
                >
                  {i.name}
                </button>
              ))}
            </div>
          )}

          {/* Only for a run that failed. A succeeded run's expansion is
              exactly what it was before this existed. */}
          {run.status === "failed" && <RunFailureBlock jobs={jobs ?? []} />}
```

- [ ] **Step 3: Type-check**

Run from the `frontend/` directory:

```bash
npx tsc --noEmit
```

Expected: no output, exit code 0.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/activity/RunLedger.tsx
git commit -m "feat(activity): show why a run failed when its ledger row is opened"
```

---

### Task 5: Style the failure block

The existing `.ledger-detail` rules are at `broadsheet.css:1425`. `--error` is
defined at line 127, and `.job-log` is already styled from its use in the
loose-job list, so it needs nothing new. `broadsheet.css` is the only stylesheet
that styles the ledger — `mobile.css` does not — so these rules have exactly one
home.

**Files:**
- Modify: `frontend/src/styles/broadsheet.css` (after the `.ledger-links` block, currently ending near line 1446)

- [ ] **Step 1: Add the rules**

In `frontend/src/styles/broadsheet.css`, immediately after the closing brace of
the `.theme-broadsheet .ledger-links` rule, add:

```css
.theme-broadsheet .ledger-failure {
  margin-top: var(--space-2);
  padding-top: var(--space-2);
  border-top: 1px solid var(--color-divider);
  font-size: 13px;
}

.theme-broadsheet .ledger-failure-shape {
  color: var(--text-faint);
  margin-bottom: var(--space-1);
}

.theme-broadsheet .ledger-failure-none {
  color: var(--text-faint);
  font-style: italic;
}

.theme-broadsheet .ledger-failure-job + .ledger-failure-job {
  margin-top: var(--space-2);
}

.theme-broadsheet .ledger-failure-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-2);
}

.theme-broadsheet .ledger-failure-role {
  font-weight: 600;
}

.theme-broadsheet .ledger-failure-error {
  color: var(--error);
  margin-top: 2px;
  overflow-wrap: anywhere;
}
```

`overflow-wrap: anywhere` on the error line matters: tool errors routinely
embed long absolute paths with no spaces, and without it a single path pushes
the ledger column wider than its grid track.

- [ ] **Step 2: Verify the variables used actually exist**

Run from the repo root:

```bash
grep -n -- "--color-divider:\|--text-faint:\|--error:\|--space-1:\|--space-2:" frontend/src/styles/broadsheet.css
```

Expected: a line for each of the five. `--color-divider` is the token every
other `border-top` in this file uses; a variable that does not resolve produces
an invisible border rather than an error, so this grep is the only thing that
catches a wrong name.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/styles/broadsheet.css
git commit -m "style(activity): style the ledger failure block"
```

---

### Task 6: Verify in the browser

This is the real verification step for this change. Do not skip it and do not
report the work as done without it.

**Files:** none

- [ ] **Step 1: Bring up the worktree stack**

Run from the worktree root:

```bash
./ops/worktree-up.sh
```

Expected: the UI on http://localhost:5273, the API on port 8100. This does
**not** disturb the main stack on 5173. Do not use bare `docker compose` here —
a hook blocks it, because relative bind mounts would repoint the main stack at
this worktree.

- [ ] **Step 2: Walk the four cases at http://localhost:5273**

Open the Activity view and check each:

1. **A failed run with a failed member job** — the row from issue #95
   (`SRR37688468.fastq.gz → GCA_019155185.1...`). Click the line. Below the
   parameters and input links you should see the shape line, the role, the
   `code: message` in red, an "Explain this error" button, and a "Show log"
   toggle that reveals the tool output.
2. **A succeeded run** — click any non-failed row. The expansion must look
   exactly as it did before this change: facts and input links, no failure
   block, no stray border.
3. **The fallback line** — a failed run whose jobs carry no error (a cancelled
   run is the easiest to produce: start a run and cancel it). Expect the
   "No job reported an error…" sentence, not an empty area.
4. **A job that wrote no log** — click "Show log" on a failed job of a type
   that does not shell out to a tool. Expect `JobLogView`'s own "No log — this
   job has not written any output yet.", not a spinner that never resolves.

- [ ] **Step 3: Confirm the backend is untouched**

Run from the worktree root:

```bash
git diff --stat main -- backend/
```

Expected: no output. This plan changes no backend file; any output here means
something went wrong.

- [ ] **Step 4: Run the backend suite as a regression check**

Run from the worktree root:

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: the same pass count as on `main`, with no failures. Read the count
rather than trusting the exit code. This is a regression check only — the change
is frontend-only, so a failure here means something unrelated is broken, not
that this work is wrong.

Use `run-worktree-tests.sh`, **not** `docker compose exec api python -m pytest`:
from a worktree, the latter silently tests `main`'s code, and it would share
Mongo with the running stack, letting two test runs wipe each other's database
mid-run.

- [ ] **Step 5: Tear down the worktree stack**

```bash
./ops/worktree-up.sh --down
```

---

### Task 7: Close out the issue

**Files:** none (GitHub only)

- [ ] **Step 1: Comment on the issue with what shipped**

```bash
gh issue comment 95 --body "Fixed. The ledger row now expands to show why a run failed.

The error was never missing from the backend -- \`run_service.status_for\` already returns each member job's error, and ActivityView already fetches every run's detail for its job counts. \`RunLedger\` took only the count and discarded the rest, so the grouping that keeps an alignment's seven jobs from filling the page also removed the only path to its diagnosis: the loose-job list below the columns is what renders errors, logs, and the explainer, and it deliberately excludes anything belonging to a run.

Opening a failed row now shows how far the run got (\"6 of 7 jobs succeeded\"), each failed job's \`code: message\`, the existing \"Explain this error\" button, and a toggle for the tool's own log -- which is where a failed aligner's real cause usually is. A failed run whose jobs carry no error at all (cancelled sibling, or jobs aged past the TTL) gets an explicit sentence saying so rather than an empty panel.

Frontend only; no API or model changes."
```

- [ ] **Step 2: Verify the comment posted**

```bash
gh issue view 95 --comments
```

Expected: the comment above appears at the end.

---

## Merging

Once Task 6 is green and Task 7 is done, this is finished work on a dev trunk —
`CLAUDE.md` is explicit that committing and merging need no permission once the
suite is green and `main` is clean. Merge to `main` and push to `origin`.

If `main` has moved while this was in progress, merge it in and re-run Task 6
Step 4 rather than assuming the earlier green still holds.

```bash
git checkout main && git merge --no-ff claude/issue-95-brainstorm-c3df6d && git push origin main
```

(Name the branch explicitly rather than using `git merge -`: the shorthand
depends on which branch you were on last, which is not something a plan can
assume about the session executing it.)

There is no `docs/TODO.md` entry for this work — it came in as issue #95 — so
no backlog entry needs moving to `docs/TODO-done.md`.
