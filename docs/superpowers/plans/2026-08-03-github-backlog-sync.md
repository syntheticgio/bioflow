# GitHub Backlog Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish every genuinely open BioFlow roadmap item and bug report as a structured GitHub issue, and add reusable issue forms and labels for future reports.

**Architecture:** Keep `docs/TODO.md` as the detailed engineering record while GitHub Issues becomes the operational tracker. Repository changes add three YAML issue forms and close one stale TODO entry; connector-backed GitHub writes create an idempotent label taxonomy and 19 open issues after duplicate searches.

**Tech Stack:** Git worktrees, GitHub issue forms (YAML), GitHub Issues API through the connected GitHub app, Markdown, Ruby's standard YAML parser, Docker-based pytest worktree runner.

---

## File map

- Create `.github/ISSUE_TEMPLATE/bug_report.yml`: structured product-defect reports.
- Create `.github/ISSUE_TEMPLATE/feature_request.yml`: structured user-facing capability requests.
- Create `.github/ISSUE_TEMPLATE/technical_task.yml`: structured maintenance and verification work.
- Create `.github/ISSUE_TEMPLATE/config.yml`: keep blank issues available for exceptional reports.
- Modify `docs/TODO.md`: remove the already-fixed index-reconciliation entry from the open backlog.
- Modify `docs/TODO-done.md`: preserve that entry with its implementation note and original diagnosis.
- Create GitHub labels and issues: external repository state, verified by querying the repository after writes.

### Task 1: Add reusable issue forms

**Files:**
- Create: `.github/ISSUE_TEMPLATE/bug_report.yml`
- Create: `.github/ISSUE_TEMPLATE/feature_request.yml`
- Create: `.github/ISSUE_TEMPLATE/technical_task.yml`
- Create: `.github/ISSUE_TEMPLATE/config.yml`

- [ ] **Step 1: Create the bug report form**

Create `.github/ISSUE_TEMPLATE/bug_report.yml` with this complete form:

```yaml
name: Bug report
description: Report behavior that is broken, missing, or inconsistent.
title: "[Bug]: "
labels:
  - "type: bug"
body:
  - type: markdown
    attributes:
      value: Thanks for reporting a BioFlow problem. Include concrete evidence where possible.
  - type: textarea
    id: observed
    attributes:
      label: What happened?
      description: Describe the observed behavior and its impact.
    validations:
      required: true
  - type: textarea
    id: reproduction
    attributes:
      label: How can we reproduce it?
      description: List the smallest sequence of steps, inputs, and project state that reproduces the problem.
      placeholder: |
        1. Open ...
        2. Run ...
        3. Observe ...
    validations:
      required: true
  - type: textarea
    id: expected
    attributes:
      label: What should have happened?
    validations:
      required: true
  - type: input
    id: environment
    attributes:
      label: Environment
      description: Host OS, architecture, browser, and BioFlow commit or image if known.
  - type: textarea
    id: evidence
    attributes:
      label: Logs, screenshots, or other evidence
      description: Remove secrets and personally identifying data before posting.
  - type: checkboxes
    id: checks
    attributes:
      label: Reporter checks
      options:
        - label: I searched open issues for an existing report.
          required: true
        - label: I removed secrets and sensitive data from the report.
          required: true
```

- [ ] **Step 2: Create the feature request form**

Create `.github/ISSUE_TEMPLATE/feature_request.yml`:

```yaml
name: Feature request
description: Propose a user-visible BioFlow capability or workflow.
title: "[Feature]: "
labels:
  - "type: feature"
body:
  - type: textarea
    id: problem
    attributes:
      label: What problem would this solve?
      description: Describe the user and workflow, not only the proposed implementation.
    validations:
      required: true
  - type: textarea
    id: outcome
    attributes:
      label: Desired outcome
      description: Describe what the user should be able to see or do.
    validations:
      required: true
  - type: textarea
    id: alternatives
    attributes:
      label: Alternatives considered
  - type: textarea
    id: scope
    attributes:
      label: Scope boundaries
      description: State what belongs in the first useful version and what does not.
  - type: textarea
    id: acceptance
    attributes:
      label: Acceptance criteria
      description: List observable conditions that would make the request complete.
    validations:
      required: true
```

- [ ] **Step 3: Create the technical-task form**

Create `.github/ISSUE_TEMPLATE/technical_task.yml`:

```yaml
name: Technical task
description: Record maintenance, verification, migration, or engineering cleanup.
title: "[Maintenance]: "
labels:
  - "type: maintenance"
body:
  - type: textarea
    id: problem
    attributes:
      label: Engineering problem
      description: Describe the invariant, gap, or maintenance burden.
    validations:
      required: true
  - type: textarea
    id: evidence
    attributes:
      label: Evidence
      description: Include measurements, failing tests, code paths, or observed behavior.
    validations:
      required: true
  - type: textarea
    id: work
    attributes:
      label: Proposed work
    validations:
      required: true
  - type: textarea
    id: risks
    attributes:
      label: Risks and constraints
  - type: textarea
    id: completion
    attributes:
      label: Completion checks
    validations:
      required: true
```

- [ ] **Step 4: Keep blank issues available**

Create `.github/ISSUE_TEMPLATE/config.yml`:

```yaml
blank_issues_enabled: true
contact_links: []
```

- [ ] **Step 5: Parse every YAML file**

Run:

```bash
ruby -e 'require "yaml"; Dir[".github/ISSUE_TEMPLATE/*.yml"].sort.each { |f| YAML.safe_load_file(f, aliases: false); puts "valid #{f}" }'
```

Expected: four `valid ...` lines and exit code 0.

- [ ] **Step 6: Check whitespace and commit**

Run:

```bash
git diff --check
git add .github/ISSUE_TEMPLATE
git commit -m "Add GitHub issue forms"
```

Expected: one commit containing exactly the four issue-template files.

### Task 2: Close the stale index-reconciliation TODO

**Files:**
- Modify: `docs/TODO.md`
- Modify: `docs/TODO-done.md`

- [ ] **Step 1: Verify the implementation named by the stale entry**

Run:

```bash
rg -n 'reconcile_indexes|await reconcile_indexes' backend/app/db/index_reconcile.py backend/app/db/client.py backend/tests/db/test_index_reconcile.py
```

Expected: the reconciler implementation, startup call before `init_beanie`, and reconciliation tests are all present.

- [ ] **Step 2: Move the entry intact to the finished backlog**

Remove the full `## Changing an index definition is a hard startup failure`
section from `docs/TODO.md`. Insert it immediately below `# Deferred findings`
in `docs/TODO-done.md`, change its heading to:

```markdown
## Changing an index definition is a hard startup failure — FIXED
```

Add this closeout note above the original body:

```markdown
Fixed before 2026-08-03 by the startup reconciliation mechanism in
`backend/app/db/index_reconcile.py`, wired through
`backend/app/db/client.py::_init_models` before `init_beanie` creates declared
indexes. On every startup it compares the live and declared key pattern,
uniqueness, sparse flag, partial filter and TTL; conflicting named indexes are
dropped and Beanie recreates them, while orphaned indexes are logged and left
alone. `backend/tests/db/test_index_reconcile.py` covers the original
`partialFilterExpression` failure and the other compared properties.

**What shipped differently:** the original entry proposed a general migration
mechanism. The implementation is deliberately narrower and automatic: it
reconciles only conflicting declared indexes, because those are the schema
changes that otherwise prevent startup, and avoids silently deleting orphaned
indexes whose intent cannot be inferred.

The original one-off migration was measured against both `biopipe` and
`biopipe_test`; the reconciler now makes the same class of upgrade idempotent
for any database initialized through `_init_models`.

Original entry follows.
```

- [ ] **Step 3: Verify the backlog split**

Run:

```bash
test "$(rg -c '^## ' docs/TODO.md)" -eq 18
test "$(rg -c '^## Changing an index definition' docs/TODO.md || true)" -eq 0
test "$(rg -c '^## Changing an index definition.*FIXED' docs/TODO-done.md)" -eq 1
git diff --check
```

Expected: every command exits 0.

- [ ] **Step 4: Commit the closeout**

Run:

```bash
git add docs/TODO.md docs/TODO-done.md
git commit -m "Close stale index reconciliation TODO"
```

Expected: one documentation-only commit.

### Task 3: Create the GitHub label taxonomy

**External state:** GitHub labels in `syntheticgio/bioflow`.

- [ ] **Step 1: Search existing labels**

Use the connected GitHub app to list repository labels. Compare by exact name;
do not delete or rename existing labels.

- [ ] **Step 2: Create missing type labels**

Create only missing labels:

| Name | Color | Description |
|---|---|---|
| `type: bug` | `d73a4a` | Confirmed or suspected incorrect behavior |
| `type: feature` | `a2eeef` | User-visible capability or workflow |
| `type: maintenance` | `fbca04` | Verification, cleanup, migration, or engineering maintenance |

- [ ] **Step 3: Create missing priority labels**

| Name | Color | Description |
|---|---|---|
| `priority: high` | `b60205` | Correctness risk, silent failure, or important blocker |
| `priority: medium` | `d93f0b` | Valuable planned work without an immediate correctness threat |
| `priority: low` | `0e8a16` | Opportunistic improvement or limitation with a workaround |

- [ ] **Step 4: Create missing area labels**

| Name | Color | Description |
|---|---|---|
| `area: frontend` | `1d76db` | React UI, browser behavior, or responsive experience |
| `area: backend` | `5319e7` | API, models, services, or queue infrastructure |
| `area: pipelines` | `0052cc` | Bioinformatics tools, runners, handlers, or suggestions |
| `area: infrastructure` | `6f42c1` | Docker, installation, deployment, storage, or resources |
| `area: provenance` | `c5def5` | Computation history, timings, methods, or traceability |
| `area: profiles` | `7057ff` | Profile ownership, sharing, or multi-user policy |

- [ ] **Step 5: Verify all 12 labels**

List labels again and assert the exact 12 names above are present. Record any
pre-existing labels reused instead of created.

### Task 4: Create 19 deduplicated GitHub issues

**External state:** GitHub issues in `syntheticgio/bioflow`.

- [ ] **Step 1: Search before creating**

Search all open issues in `syntheticgio/bioflow`. For each title in the table
below, reuse an exact-title match and add any missing planned labels; otherwise
create a new issue. Never create a second issue with the same exact title.

- [ ] **Step 2: Create the roadmap and bug issues**

Every body must contain `## Problem`, `## Scope`, `## Acceptance criteria`,
and `## Source`. The source for TODO-backed issues is the corresponding heading
in `docs/TODO.md` on `main`.

| # | Exact title | Labels | Acceptance criteria summary |
|---|---|---|---|
| 1 | Notify on new feedback submissions | `type: feature`, `priority: medium`, `area: backend` | Best-effort delivery after persistence; credentials in settings; notification failure never changes the saved submission's 201 response. |
| 2 | Add a focused mobile view for activity and NCBI downloads | `type: feature`, `priority: medium`, `area: frontend` | Under ~600px, users can inspect job progress and dispatch SRA downloads; desktop analysis workflows remain out of scope. |
| 3 | Share files between profiles without copying bytes | `type: feature`, `priority: medium`, `area: profiles`, `area: backend` | Shares reuse blob digests; offer/revoke policy is explicit; recipient location and owner-deletion behavior are defined and tested. |
| 4 | Build a native BioFlow installer and launcher | `type: feature`, `priority: medium`, `area: infrastructure` | Detect Docker, collect storage/install/port settings, write Compose configuration, start/stop BioFlow, open first-run UI, and optionally pre-pull tool images. |
| 5 | Support post-install downloads for optional tools | `type: feature`, `priority: medium`, `area: infrastructure`, `area: pipelines` | Candidate tools and image strategy are defined; running UI can install them; full-install prefetch remains compatible; suggestions reflect availability. |
| 6 | Add job progress reporting and resource transparency | `type: feature`, `priority: medium`, `area: backend`, `area: pipelines` | Define a common progress vocabulary; parse or instrument representative tools; surface live progress and resource information; restart persistence policy is explicit. |
| 7 | Add configurable resource limits and intelligent enforcement | `type: feature`, `priority: high`, `area: infrastructure`, `area: backend` | Configure memory/CPU/thread limits; combine Docker enforcement with admission thresholds; document which guarantees are hard versus advisory. |
| 8 | Segment timing models by thread count | `type: maintenance`, `priority: low`, `area: backend`, `area: provenance` | Fit real rows by thread count when sample size permits; fall back to byte-only model; duration and memory consumers continue filtering failed runs. |
| 9 | Add per-object computation provenance | `type: feature`, `priority: medium`, `area: provenance`, `area: frontend`, `area: backend` | Expose `records_for_object()` and render duration, RSS, threads, tool/version, machine, and outcome, including failures. |
| 10 | Investigate disappearing QC report directories | `type: bug`, `priority: high`, `area: backend`, `area: pipelines` | Log every deletion with caller/object/path/reason; retain evidence for days; reproduce or identify the removal path; prevent facts from silently pointing at missing reports. |
| 11 | Add DRAGMAP aligner support | `type: feature`, `priority: low`, `area: pipelines` | Verify arm64 support; register runner/tool metadata; add suggestion behavior; test availability-off as well as installed behavior. |
| 12 | Audit hand-maintained tool registries | `type: maintenance`, `priority: high`, `area: backend`, `area: pipelines` | Inventory named registries; derive mappings where safe; otherwise add exhaustive enum-member tests; missing registrations cannot silently skip artifacts. |
| 13 | Complete the remaining post-assembly QC workflows | `type: feature`, `priority: medium`, `area: pipelines` | Add reference-based misassembly detection; sequence CRAQ/GCI/Merqury after assembly realignment support; keep contamination screening explicitly separate. |
| 14 | Add reference-guided assembly workflows | `type: feature`, `priority: medium`, `area: pipelines` | Design and implement Pilon, RagTag, and iVar with distinct chemistry/context suggestion rules and provenance-safe inputs. |
| 15 | Verify the in-app DESeq2 workflow end to end | `type: maintenance`, `priority: high`, `area: pipelines`, `area: frontend` | Run a real 2x2 replicated RNA-seq project through dialog, queue, applier, results object, tables, and charts; record measured output and fix surfaced defects separately or in scope. |
| 16 | Add reusable user-defined pipeline DAGs | `type: feature`, `priority: medium`, `area: backend`, `area: frontend`, `area: pipelines` | Persist workflow definitions/instances; model dependencies, retries, failure semantics, restart recovery, and aggregate progress. |
| 17 | Generate fact-grounded pipeline provenance narratives | `type: feature`, `priority: low`, `area: provenance`, `area: backend` | Walk recorded provenance to source inputs; assemble only recorded steps/tool versions; use the model for prose without allowing invented methods. |
| 18 | Improve paired-read detection beyond filenames | `type: feature`, `priority: low`, `area: pipelines` | Preserve filename fast path and manual override; add authoritative read-ID validation or a scoped alternative; ambiguous/unrelated pairs never link silently. |
| 19 | Make feedback submission ordering deterministic | `type: maintenance`, `priority: low`, `area: backend` | Reproduce or explain the intermittent reverse order; define a deterministic secondary sort; add a stable regression test; full suite passes repeatedly. |

For issue 19, record exact evidence: the first clean worktree suite run failed
`TestListFeedback.test_lists_submissions_newest_first` with `['first',
'second']`, the isolated rerun passed, and the second full run passed 2,564
tests. Its source is the test and route rather than `docs/TODO.md`.

- [ ] **Step 3: Verify issue contents after creation**

Search open issues again and verify:

- exactly one open issue exists for every exact title above;
- all expected labels are present on each issue;
- each TODO-backed body links to the matching `docs/TODO.md` heading;
- issue 19 cites the failing test, passing isolated rerun, and passing full rerun;
- every issue has a GitHub URL.

Save a compact title/number/URL list for the final handoff.

### Task 5: Verify, publish, merge, and push

**Files:** all files changed by Tasks 1 and 2, plus the committed design and
implementation plan.

- [ ] **Step 1: Validate repository state and forms**

Run:

```bash
git diff --check
ruby -e 'require "yaml"; Dir[".github/ISSUE_TEMPLATE/*.yml"].sort.each { |f| YAML.safe_load_file(f, aliases: false); puts "valid #{f}" }'
git status --short
```

Expected: no whitespace errors, four valid YAML files, and only the plan file
uncommitted if earlier task commits were made as specified.

- [ ] **Step 2: Commit the implementation plan**

Run:

```bash
git add docs/superpowers/plans/2026-08-03-github-backlog-sync.md
git commit -m "Plan GitHub backlog sync"
```

Expected: the branch contains four separable commits: design, issue forms,
TODO closeout, and implementation plan. If the plan commit happens before
execution, retain that order and do not rewrite history.

- [ ] **Step 3: Run the complete backend suite in the worktree**

Run:

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: `2564 passed`, zero failures. If the feedback ordering test flakes,
record the new run on issue 19 and rerun the complete suite; do not merge until
a complete run is green.

- [ ] **Step 4: Verify branch diff and GitHub state**

Run:

```bash
git status --short
git log --oneline main..HEAD
git diff --stat main...HEAD
```

Expected: clean worktree, only approved files in the diff, and all expected
commits. Query GitHub one final time for all 19 exact titles and 12 labels.

- [ ] **Step 5: Push the feature branch**

Run:

```bash
git push -u origin codex/github-backlog-sync
```

Expected: the remote branch is created and tracking is configured.

- [ ] **Step 6: Merge into clean main**

In the main checkout, first run:

```bash
git status --short
git branch --show-current
```

Expected: branch `main`; only the user's pre-existing untracked `.codex/`,
`AGENTS.md`, and `docs/BUGS_UX.md` may be present. They must not be staged.

Then merge:

```bash
git merge --no-ff codex/github-backlog-sync -m "Merge GitHub backlog sync"
```

Expected: clean merge with the user's untracked files untouched.

- [ ] **Step 7: Verify after merge if main moved**

If `main` changed after the worktree suite began, run from the main checkout:

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: the complete suite passes. If `main` did not move except for this
merge, the worktree's complete green run is the merge evidence.

- [ ] **Step 8: Push main and verify remote state**

Run:

```bash
git push origin main
git status --short
git log -1 --oneline
```

Expected: `origin/main` advances to the merge commit; only the user's original
untracked files remain; GitHub exposes the issue forms, 12 labels, and 19 open
issues.
