# GitHub backlog sync design

**Date:** 2026-08-03

## Goal

Make GitHub Issues the actionable tracker for BioFlow's open roadmap and bug
reports without discarding the deeper diagnostic history in `docs/TODO.md`.
The repository will gain structured issue forms, a small reusable label
taxonomy, and one issue for every genuinely open backlog heading.

## Source of truth and scope

`docs/TODO.md` remains the detailed engineering record. GitHub issues become
the operational view: status, priority, labels, discussion, and closure. Each
synced issue links back to the matching TODO heading and contains enough scope
and acceptance criteria to be useful without opening the file first.

The current TODO has 19 headings. The entry "Changing an index definition is
a hard startup failure" is no longer open because startup reconciliation now
lives in `backend/app/db/index_reconcile.py` and is called by
`backend/app/db/client.py`. That entry will be marked fixed and moved intact to
`docs/TODO-done.md`, leaving 18 active headings to publish.

The clean worktree baseline also produced one intermittent feedback-ordering
test failure before passing both in isolation and on a complete rerun. It will
be recorded as a separate maintenance issue with the observed evidence rather
than presented as a confirmed product defect.

## Issue mapping

One issue will be created for each active heading, followed by the newly
observed maintenance issue:

1. Notify on new feedback submissions
2. Add a focused mobile view for activity and NCBI downloads
3. Share files between profiles without copying bytes
4. Build a native BioFlow installer and launcher
5. Support post-install downloads for optional tools
6. Add job progress reporting and resource transparency
7. Add configurable resource limits and intelligent enforcement
8. Segment timing models by thread count
9. Add per-object computation provenance
10. Investigate disappearing QC report directories
11. Add DRAGMAP aligner support
12. Audit hand-maintained tool registries
13. Complete the remaining post-assembly QC workflows
14. Add reference-guided assembly workflows
15. Verify the in-app DESeq2 workflow end to end
16. Add reusable user-defined pipeline DAGs
17. Generate fact-grounded pipeline provenance narratives
18. Improve paired-read detection beyond filenames
19. Make feedback submission ordering deterministic

Items 11 and 13 come from partially completed umbrella entries: their issues
describe only the work that remains. The list has 19 issue titles: 18 active
TODO headings and the intermittent feedback-ordering maintenance issue. The
final implementation must derive and report the actual created count from
GitHub rather than relying on this prose.

## Labels

Labels use three independent dimensions so an issue can be filtered without a
large bespoke taxonomy:

- Type: `type: bug`, `type: feature`, `type: maintenance`
- Priority: `priority: high`, `priority: medium`, `priority: low`
- Area: `area: frontend`, `area: backend`, `area: pipelines`,
  `area: infrastructure`, `area: provenance`, `area: profiles`

The disappearing QC reports are high-priority bugs. Verification gaps and
silent registry drift are maintenance work. User-visible capabilities are
features. Priorities otherwise reflect correctness and dependency value, not
estimated effort.

No milestone will be created. Milestones imply a release or dated delivery
boundary, and the repository currently defines neither. Labels and the open
issue list are sufficient until a release boundary exists.

## Issue forms

Three YAML issue forms will be added under `.github/ISSUE_TEMPLATE/`:

- `bug_report.yml`: observed behavior, reproduction, expected behavior,
  impact, environment, and logs
- `feature_request.yml`: problem, proposed outcome, alternatives, scope, and
  acceptance criteria
- `technical_task.yml`: engineering problem, evidence, proposed work,
  risks, and completion checks

The forms apply the corresponding type label automatically. Blank issues stay
enabled for reports that do not fit a form.

## Publishing and verification

Work happens on `codex/github-backlog-sync` in an isolated worktree. The
implementation will:

1. Add and validate the issue-form YAML.
2. Close out the stale index-reconciliation TODO entry.
3. Create or reconcile labels idempotently.
4. Search for matching open issues before each creation to avoid duplicates.
5. Create each issue through the connected GitHub app.
6. Query GitHub again and verify every expected title, label, and URL.
7. Run the full repository-prescribed worktree test suite.
8. Commit, merge to a clean `main`, rerun the suite after merge if `main`
   moved, and push `main` to `origin`.

Issue creation is external state and is not rolled back if a later local Git
step fails. Every call therefore uses a deterministic title and duplicate
search first, making recovery safe.
