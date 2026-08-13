# Results Shared Infrastructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract the duplicated report-path containment guard and the frontend compute-results lifecycle shared across the BAM, VCF, and annotation Results views, without changing user-visible behavior except for one deliberate `Stat` styling consolidation.

**Architecture:** Two independent units. Backend: one `resolve_report_file()` helper in `app/storage/paths.py` (which already hosts `resolve_registerable`, the same resolve-then-contain shape), adopted by the two report endpoints. Frontend: one `useComputeResults()` hook plus one shared `Stat` component, adopted by three Results views. The units do not depend on each other and can be built in either order.

**Tech Stack:** FastAPI + Beanie (backend), pytest, React + TanStack Query (frontend), TypeScript.

**Spec:** [`docs/superpowers/specs/2026-08-13-results-shared-infrastructure-design.md`](../specs/2026-08-13-results-shared-infrastructure-design.md)

---

## Background an implementer needs

**Running the tests.** This work happens in a git worktree. Do **not** use
`docker compose exec api pytest` — from a worktree that silently tests `main`'s
code, not yours. Use:

```bash
./backend/run-worktree-tests.sh tests/storage/test_paths.py -q
```

**The two endpoints being changed** are `get_bam_stats_report`
(`backend/app/api/v1/pipelines.py:686`) and `get_vcf_stats_report`
(`backend/app/api/v1/pipelines.py:892`). Both currently inline a three-step
guard: reject `..`/empty/absolute segments, resolve, re-check containment. They
spell the third step differently (`target.is_relative_to(root)` vs
`root not in target.parents`). These agree on every reachable input — this is a
tidy-up, **not** a security fix, and the endpoints' observable behavior must not
change.

**`.is_file()` is load-bearing** in both current spellings and must survive into
the helper. It is what rejects the report root itself and what rejects a
symlink pointing outside the tree.

**Existing test coverage is the regression net.** `test_bam_stats_reports.py`
and `test_vcf_stats_report.py` both already have traversal tests. They must pass
**unmodified** after the refactor. If a step tempts you to edit them, the
refactor is wrong.

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/app/storage/paths.py` | **Modify.** Add `resolve_report_file()` alongside the existing containment helpers. |
| `backend/tests/storage/test_paths.py` | **Modify.** Add `TestResolveReportFile` covering the four cases neither endpoint tests. |
| `backend/app/api/v1/pipelines.py` | **Modify.** Two endpoints adopt the helper (`:686`, `:892`). |
| `frontend/src/hooks/useComputeResults.ts` | **Create.** The mutation triad shared by three views. |
| `frontend/src/components/Stat.tsx` | **Create.** One summary-statistic tile. |
| `frontend/src/components/BamResults.tsx` | **Modify.** Adopt hook; delete local `Stat` (`:294`). |
| `frontend/src/components/VariantResults.tsx` | **Modify.** Adopt hook; delete local `Stat` (`:243`). |
| `frontend/src/components/AnnotationResults.tsx` | **Modify.** Adopt hook. |

---

## Task 1: `resolve_report_file()` helper

Implements spec R1, R2, R3.

**Files:**
- Modify: `backend/app/storage/paths.py`
- Test: `backend/tests/storage/test_paths.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/storage/test_paths.py`. Note the import line at the top
of the file must also gain `resolve_report_file`:

```python
from app.storage.paths import (
    blob_rel_path,
    resolve_registerable,
    resolve_report_file,
    validate_sha256,
)
```

Then append this class:

```python
class TestResolveReportFile:
    """Containment for client-supplied report paths.

    These are the cases neither report endpoint covers today. The endpoints'
    own traversal suites cover the ordinary `../` attacks; what is pinned here
    is the behavior that both call sites currently get only incidentally, via
    an `.is_file()` that happens to be ANDed in.
    """

    @pytest.fixture
    def root(self, tmp_path):
        (tmp_path / "report.tsv").write_text("col\tval\n")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "nested.tsv").write_text("col\tval\n")
        return tmp_path

    def test_returns_a_file_directly_under_the_root(self, root):
        assert resolve_report_file(root, "report.tsv") == root / "report.tsv"

    def test_returns_a_file_in_a_subdirectory(self, root):
        assert resolve_report_file(root, "sub/nested.tsv") == root / "sub" / "nested.tsv"

    @pytest.mark.parametrize(
        "bad",
        [
            "",  # empty: resolves to the root directory itself
            "..",
            "../secret.tsv",
            "sub/../../secret.tsv",
            "/etc/passwd",
        ],
    )
    def test_rejects_traversal_and_absolute_paths(self, root, bad):
        with pytest.raises(NotFoundError):
            resolve_report_file(root, bad)

    def test_rejects_the_root_itself(self, root):
        """A directory is not a report. Both call sites rely on this today."""
        with pytest.raises(NotFoundError):
            resolve_report_file(root, ".")

    def test_rejects_a_directory(self, root):
        with pytest.raises(NotFoundError):
            resolve_report_file(root, "sub")

    def test_rejects_a_missing_file(self, root):
        with pytest.raises(NotFoundError):
            resolve_report_file(root, "nope.tsv")

    def test_rejects_a_symlink_escaping_the_root(self, root, tmp_path_factory):
        """The one input the `..` prefilter does not catch.

        Both endpoints reject this today only because `.is_file()` follows the
        link to a path outside the root. Pinning it makes that explicit.
        """
        outside = tmp_path_factory.mktemp("outside")
        secret = outside / "secret.tsv"
        secret.write_text("private\n")
        (root / "link.tsv").symlink_to(secret)

        with pytest.raises(NotFoundError):
            resolve_report_file(root, "link.tsv")
```

`NotFoundError` must be imported in the test file. Check the top of
`test_paths.py` — it currently imports only `ValidationError`. Change that line
to:

```python
from app.errors import NotFoundError, ValidationError
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/storage/test_paths.py -q
```

Expected: collection error — `ImportError: cannot import name 'resolve_report_file'`.

- [ ] **Step 3: Write the implementation**

Add to `backend/app/storage/paths.py`. It needs `PurePosixPath` and
`NotFoundError`, so update the imports at the top of the file:

```python
from pathlib import Path, PurePosixPath

from app.config import settings
from app.errors import NotFoundError, ValidationError
```

Then append the function:

```python
def resolve_report_file(root: Path, report_path: str) -> Path:
    """Resolve a client-supplied report path inside `root`, or raise.

    Three steps, in this order. Segments are rejected textually first, so a
    crafted path never reaches the filesystem. The result is then resolved and
    re-checked against the root, which is what catches a symlink whose target
    escapes the tree -- the one case the textual pass cannot see. Finally the
    target must be a regular file: that is what rejects the root directory
    itself, and it is load-bearing rather than decorative.

    Raises NotFoundError rather than a permission error for every rejection:
    the caller has already proven it owns the object, so the only thing a
    distinct status code would reveal is whether a given path exists.
    """
    parts = PurePosixPath(report_path).parts
    if any(p in ("..", "") for p in parts) or PurePosixPath(report_path).is_absolute():
        raise NotFoundError(f"No such report: {report_path}")

    target = (root / report_path).resolve()
    if not target.is_relative_to(root.resolve()) or not target.is_file():
        raise NotFoundError(f"No such report: {report_path}")

    return target
```

Note: `PurePosixPath("").parts` is `()` and `PurePosixPath(".").parts` is `()`,
so neither is caught by the `any(...)` check — both are rejected at the
`is_file()` step, since each resolves to the root directory. The parametrized
empty-string case and `test_rejects_the_root_itself` both pin that.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/storage/test_paths.py -q
```

Expected: all pass. Confirm the count went up rather than only reading the exit
code.

- [ ] **Step 5: Commit**

```bash
git add backend/app/storage/paths.py backend/tests/storage/test_paths.py
git commit -m "feat(storage): add resolve_report_file containment helper

Both stats report endpoints hand-roll the same three-step guard with two
different spellings of the containment check. Extract it so a third result
type inherits the guard rather than re-deriving it -- .is_file() is
load-bearing in both current spellings and easy to drop when copying.

Covers the cases neither endpoint tests today: an empty path, the root
itself, a directory, and a symlink escaping the root.

Refs #299

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 2: Adopt the helper in both report endpoints

Implements spec R4.

**Files:**
- Modify: `backend/app/api/v1/pipelines.py:686-749` and `:892-922`

- [ ] **Step 1: Confirm the existing endpoint tests pass before touching anything**

```bash
./backend/run-worktree-tests.sh tests/api/test_bam_stats_reports.py tests/api/test_vcf_stats_report.py -q
```

Expected: all pass. Record the count — it must be identical at Step 4.

- [ ] **Step 2: Rewrite `get_bam_stats_report`'s guard**

In `backend/app/api/v1/pipelines.py`, find this block inside
`get_bam_stats_report` (currently lines 714-721):

```python
    parts = PurePosixPath(report_path).parts
    if any(p in ("..", "") for p in parts) or PurePosixPath(report_path).is_absolute():
        raise NotFoundError(f"No such report: {report_path}")

    root = (settings.bam_stats_dir / str(object_id)).resolve()
    target = (root / report_path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise NotFoundError(f"No such report: {report_path}")
```

Replace it with:

```python
    root = settings.bam_stats_dir / str(object_id)
    target = resolve_report_file(root, report_path)
```

Then update that function's docstring. Replace the paragraph beginning
"Same containment rules as get_qc_report" with:

```
    Containment is `resolve_report_file`'s: `..` and absolute paths are
    rejected textually, then the resolved path is re-checked against the report
    root. The object is resolved under the caller's profile first, so a
    directory named by object id is not itself the access rule. Unlike a QC
    report, this file is generated by this app from numeric samtools output
    rather than embedding read-derived strings, and it is never rendered as a
    document -- so the sandboxed CSP that HTML report serving needs does not
    apply here.
```

- [ ] **Step 3: Rewrite `get_vcf_stats_report`'s guard**

Find this block inside `get_vcf_stats_report` (currently lines 908-915):

```python
    parts = PurePosixPath(report_path).parts
    if any(p in ("..", "") for p in parts) or PurePosixPath(report_path).is_absolute():
        raise NotFoundError(f"No such report: {report_path}")

    root = (settings.vcf_stats_dir / str(object_id)).resolve()
    target = (root / report_path).resolve()
    if not target.is_file() or root not in target.parents:
        raise NotFoundError(f"No such report: {report_path}")
```

Replace it with:

```python
    root = settings.vcf_stats_dir / str(object_id)
    target = resolve_report_file(root, report_path)
```

Then update that function's docstring. Replace the paragraph beginning
"Same containment rules as get_bam_stats_report" with:

```
    Containment is `resolve_report_file`'s, the same helper the BAM report
    route uses -- the object is resolved under the caller's profile, then `..`
    and absolute paths are rejected textually, then the resolved path is
    re-checked against the report root.
```

- [ ] **Step 4: Add the import and remove the now-unused one**

At the top of `pipelines.py`, add `resolve_report_file` to the existing
`app.storage.paths` import on line 36:

```python
from app.storage.paths import blob_path, resolve_report_file
```

`PurePosixPath` may now be unused in this file. Check before removing it:

```bash
grep -n "PurePosixPath" backend/app/api/v1/pipelines.py
```

If the only remaining hit is the import on line 4, change that line to:

```python
from pathlib import Path
```

If other call sites still use it, leave the import alone.

- [ ] **Step 5: Run the endpoint tests — they must pass unmodified**

```bash
./backend/run-worktree-tests.sh tests/api/test_bam_stats_reports.py tests/api/test_vcf_stats_report.py -q
```

Expected: PASS, at the identical count recorded in Step 1. **Do not edit these
test files.** If one fails, the refactor changed behavior — revert and
reconsider rather than adjusting the test.

- [ ] **Step 6: Run ruff, since CI checks import order and it is not run locally by pytest**

```bash
./backend/run-worktree-tests.sh --ruff 2>/dev/null || docker run --rm -v "$PWD/backend:/w" -w /w ghcr.io/astral-sh/ruff:latest check app/api/v1/pipelines.py app/storage/paths.py
```

Expected: `All checks passed!`. If `I001` fires, apply the fix ruff itself
suggests — this exact rule caught a real bug on #217/#314 that the local suite
missed.

- [ ] **Step 7: Commit**

```bash
git add backend/app/api/v1/pipelines.py
git commit -m "refactor(api): route both stats report endpoints through one containment helper

The two endpoints spelled the final containment check differently
(is_relative_to vs. root-in-parents). Both are correct on every reachable
input -- .is_file() blocks the one case that would separate them -- so this
is behavior-preserving, verified by both traversal suites passing unmodified.

Refs #299

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 3: `useComputeResults()` hook

Implements spec R5.

**Files:**
- Create: `frontend/src/hooks/useComputeResults.ts`
- Modify: `frontend/src/components/BamResults.tsx:34-41`
- Modify: `frontend/src/components/VariantResults.tsx:22-29`
- Modify: `frontend/src/components/AnnotationResults.tsx:34-41`

There is no component-testing setup in this repo (no jsdom, zero `.test.tsx`
files) and none is expected. Verification is the type-check plus manual
checking at the running app, per CLAUDE.md.

- [ ] **Step 1: Create the hook**

Create `frontend/src/hooks/useComputeResults.ts`:

```typescript
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { notify } from "../stores/messageStore";

/**
 * The compute-results mutation shared by every on-demand Results view.
 *
 * All three launch endpoints take the same (objectId, targetNode?) shape and
 * queue a job, so the only thing that varies between views is which one to
 * call. Invalidating ["jobs"] is what makes the queued job appear without a
 * refresh; the toast is what tells the user the click landed, since the view
 * itself does not change until the job finishes.
 */
export function useComputeResults(
  objectId: string,
  targetNode: string,
  launch: (objectId: string, targetNode?: string) => Promise<unknown>,
) {
  const qc = useQueryClient();

  return useMutation({
    mutationFn: () => launch(objectId, targetNode || undefined),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      notify.info("Computing results");
    },
    onError: (e: Error) => notify.error(e.message),
  });
}
```

- [ ] **Step 2: Adopt in `BamResults.tsx`**

Replace lines 34-41 (the `const compute = useMutation({...})` block) with:

```typescript
  const compute = useComputeResults(obj.id, targetNode, api.launchBamStats);
```

Update the imports at the top of the file. Remove `useMutation` and
`useQueryClient` from the `@tanstack/react-query` import — check whether the
file uses them elsewhere first:

```bash
grep -n "useMutation\|useQueryClient\|notify" frontend/src/components/BamResults.tsx
```

If the only remaining hits are the import lines and the now-deleted block,
delete line 1 (`import { useMutation, useQueryClient } from "@tanstack/react-query";`)
and the `notify` import on line 4 entirely, then add:

```typescript
import { useComputeResults } from "../hooks/useComputeResults";
```

Also delete the now-unused `const qc = useQueryClient();` on line 30.

- [ ] **Step 3: Adopt in `VariantResults.tsx`**

Replace lines 22-29 with:

```typescript
  const compute = useComputeResults(obj.id, targetNode, api.launchVcfStats);
```

Delete `const qc = useQueryClient();` on line 18. Apply the same import cleanup
as Step 2, checking first:

```bash
grep -n "useMutation\|useQueryClient\|notify" frontend/src/components/VariantResults.tsx
```

Add:

```typescript
import { useComputeResults } from "../hooks/useComputeResults";
```

- [ ] **Step 4: Adopt in `AnnotationResults.tsx`**

Replace lines 34-41 with:

```typescript
  const compute = useComputeResults(obj.id, targetNode, api.launchAnnotationStats);
```

Delete `const qc = useQueryClient();` on line 28. Apply the same import cleanup,
checking first:

```bash
grep -n "useMutation\|useQueryClient\|notify" frontend/src/components/AnnotationResults.tsx
```

Add:

```typescript
import { useComputeResults } from "../hooks/useComputeResults";
```

- [ ] **Step 5: Type-check**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors. An unused-import error here means Step 2-4's cleanup
missed one — fix it rather than suppressing.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/hooks/useComputeResults.ts frontend/src/components/BamResults.tsx frontend/src/components/VariantResults.tsx frontend/src/components/AnnotationResults.tsx
git commit -m "refactor(ui): share the compute-results mutation across Results views

The BAM, VCF, and annotation views each defined the same mutation, toast,
and jobs invalidation. All three launch endpoints take the same shape, so
the only thing that varied was which one to call.

Refs #299

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 4: Consolidate `Stat`

Implements spec R6. **This is the only user-visible change in the plan** — it is
isolated in its own commit so it can be reverted without touching Tasks 1-3.

**Files:**
- Create: `frontend/src/components/Stat.tsx`
- Modify: `frontend/src/components/BamResults.tsx` (delete local `Stat` at `:294`)
- Modify: `frontend/src/components/VariantResults.tsx` (delete local `Stat` at `:243`)

The two current definitions take identical props and differ only in styling.
`VariantResults`' treatment wins, per the spec: uppercase tracked label, 22px
value. `BamResults`' summary row will therefore render visibly larger than
before.

- [ ] **Step 1: Create the shared component**

Create `frontend/src/components/Stat.tsx`:

```typescript
/**
 * One summary statistic: a small label above a large value.
 *
 * Previously defined twice, in BamResults and VariantResults, with the same
 * props and different type scales -- so two views a user reads as a set
 * rendered their headline numbers differently. This is VariantResults'
 * treatment, the newer of the two.
 */
export function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div
        style={{
          textTransform: "uppercase",
          fontSize: 11,
          letterSpacing: "0.06em",
          color: "var(--text-faint)",
        }}
      >
        {label}
      </div>
      <div style={{ fontSize: 22, fontWeight: 600 }}>{value}</div>
    </div>
  );
}
```

- [ ] **Step 2: Delete the local `Stat` from `VariantResults.tsx`**

Delete the whole `function Stat({ label, value }: ...)` block at lines 243-259.
Add to the imports:

```typescript
import { Stat } from "./Stat";
```

- [ ] **Step 3: Delete the local `Stat` from `BamResults.tsx`**

Delete the whole `function Stat({ label, value }: ...)` block at lines 294-301.
Add to the imports:

```typescript
import { Stat } from "./Stat";
```

- [ ] **Step 4: Type-check**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 5: Verify in the running app**

Bring up this worktree's stack (not plain `docker compose`, which would
repoint the main instance at this branch):

```bash
./ops/worktree-up.sh
```

Open http://localhost:5273. For a BAM object and a VCF object with computed
results, open the Results tab and confirm:

- Both summary rows render with the same type scale.
- The BAM row's numbers are now larger than before, and its labels are
  uppercase — this is the intended change, not a regression.
- "Compute results" on an object without results still queues a job and shows
  the "Computing results" toast (this exercises Task 3).

- [ ] **Step 6: Tear down the worktree stack**

Per CLAUDE.md, a stack you brought up for testing is yours to bring back down —
orphaned stacks with live Mongos corrupt other test runs.

```bash
./ops/worktree-up.sh --down
```

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/Stat.tsx frontend/src/components/BamResults.tsx frontend/src/components/VariantResults.tsx
git commit -m "tweak(ui): render BAM and variant summary stats at one type scale

Stat was defined twice with identical props and different styling, so the
BAM summary row rendered its headline numbers at 12px against the variant
view's 22px. Adopts the variant treatment for both.

Visible change: the BAM summary row is now larger, with uppercase labels.

Refs #299

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 5: Full suite, push, and PR

- [ ] **Step 1: Run the full backend suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: green. Read the pass/fail **count**, not just the exit code. If the
run dies with `EXIT=137`, that is host memory pressure from concurrent stacks,
not a test failure — check for orphaned stacks with
`./ops/worktree-up.sh --list` and prune before rerunning.

- [ ] **Step 2: Push**

```bash
git push -u origin HEAD
```

- [ ] **Step 3: Open the PR**

```bash
gh pr create --base main --title "refactor(results): share the report-path guard and compute lifecycle across result views" --body "$(cat <<'EOF'
Follow-up to #257, implementing the extraction scoped in
`docs/superpowers/specs/2026-08-13-results-shared-infrastructure-design.md`.

## Why

The BAM, VCF, and annotation Results views were built in sequence, each
reusing the previous one's shape by hand. This extracts the two pieces that
have real consumers and a stable interface, and records why the other
candidates were left alone.

**Report path containment.** Both stats report endpoints hand-rolled the same
three-step guard with two different spellings of the final check. They agree
on every reachable input -- `.is_file()` blocks the one case that would
separate them -- so this is behavior-preserving, not a security fix. It is
worth doing because `.is_file()` is silently load-bearing in both spellings,
and a third result type copying either one inherits that. Both endpoints'
existing traversal suites pass unmodified, which is the regression proof.

**Compute lifecycle.** Three views each defined the same mutation, toast, and
jobs invalidation.

## Deliberately not done

- **No shared result table.** `VariantTable` and `AnnotationFeatureTable`
  share ~30 lines of paging bookkeeping against ~1,000 lines of
  format-specific behavior. Coupling them is the non-goal #299 names.
- **No shared index-path preamble.** Five endpoints repeat a ~6-line
  resolve/404 block, but the real repetition is in the docstrings rather than
  the logic.

## Visible change

One, isolated in its own commit: `Stat` was defined twice with identical props
and different type scales, so the BAM summary row rendered at 12px against the
variant view's 22px. Both now use the variant treatment, making the BAM row
larger with uppercase labels.

Closes #299

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Label the PR**

`.github/release.yml` categorizes release notes by label, not by the title
prefix — an unlabelled PR lands under "Other changes".

```bash
gh pr edit --add-label "type:refactor" --add-label "area:pipelines" --add-label "area:frontend"
```

If `type:refactor` does not exist, list what does and pick the closest:

```bash
gh label list --limit 100
```

- [ ] **Step 5: Watch CI to completion**

`gh pr create` returns before any check runs. Poll until every check reports a
terminal state — a "pending" read seconds after creation means the run has not
started, not that it is done.

```bash
gh pr checks --watch
```

Then confirm there is no conflict:

```bash
gh pr view --json mergeable,mergeStateStatus
```

`UNSTABLE` means checks are still running — keep waiting. A real conflict means
rebase on `origin/main` and push again. Only once checks are green and
`mergeable` is clean is the task done: report the PR URL and stop. **Do not
merge** — that is the user's call.

---

## Notes for the implementer

**If a report endpoint test fails in Task 2**, the extraction changed behavior.
The helper is meant to be a pure consolidation. Do not adjust the test to match
the new code — revert, and work out which of the two original spellings the
helper diverged from.

**`docs/TODO.md` needs no update.** This work comes from issue #299, not a
backlog entry. Check anyway with `grep -n "299\|result view" docs/TODO.md`; if
an entry does describe this, append ` — FIXED` with a note and move the whole
entry to `docs/TODO-done.md` per CLAUDE.md.
