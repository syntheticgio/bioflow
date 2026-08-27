# Working on this repo

Single-user, local-only tool for non-critical, non-essential work. Optimize for
"the person using it can see their change" over correctness-at-scale practices
that make sense for a team or a hosted service.

**`main` is this project's dev trunk, not its production branch.** Shipping
happens at a tag, from a release branch, several stages downstream (see
[Release methodology](#release-methodology)). Landing on `main` is not shipping.

> Rules here are deliberately terse. The incidents behind them -- what broke,
> when, what it cost -- are in [docs/agent-notes.md](docs/agent-notes.md),
> linked per section. Read a rule here; go there when it looks arbitrary.

## Finish work: branch, push, PR, merge when green

**Merge your own work to `main` once every check passes.** Committing,
pushing, opening the PR and merging are all yours to do without asking. The
end state of a task is a merged PR.

**Green CI is the whole gate, so the checks must have actually finished.**
"Green" means every check reports `pass` -- not `pending`, not "the ones I
looked at". Poll `gh pr checks <N>` until every check reports, and
`gh pr view <N> --json mergeable,mergeStateStatus` for conflicts.
`UNSTABLE` means checks are still running; keep waiting.

Before opening the PR, catch up to `main` yourself:

```bash
git fetch origin main
git rebase origin/main
```

If the rebase conflicts in a way that isn't a quick per-commit fix, `git
rebase --abort` and `git merge origin/main` instead. Either way, read both
sides and keep what's correct -- don't take a side blindly.

**Then verify your changes survived**, because a conflict resolution can
silently drop a hunk:

```bash
git diff origin/main...HEAD --stat
```

Check the file list matches what you intended and skim for anything reverted
or missing. Then:

```bash
git push -u origin HEAD
gh pr create --base main --fill
gh pr merge <N> --rebase --delete-branch
```

**`--rebase`, never `--squash`** -- the changelog is generated from commit
subjects *and bodies*, which a squash destroys. **Never `--auto`**: it
returns before the checks pass, so the task ends with you having verified
nothing.

Keep commits separable. A mechanical rename and a behaviour change in one
commit is a commit nobody can review or revert. Self-merging raises the
stakes: a separable commit is what makes the change reviewable *after* the
fact, and reverting one commit without unpicking three is the safety net
that replaces the review gate.

**If the task ran in a worktree, remove it once the merge lands** --
`./ops/worktree-up.sh --down` first, then `ExitWorktree` (`action: "remove"`)
or `git worktree remove <path>`.

Then report the merge and the PR URL, and stop.

What earns a stop-and-ask instead of a merge:

- **A red check.** Fix it and re-poll; don't merge around it.
- **A merge conflict** (`DIRTY`). Rebase and push -- ordinary work.
- **A change the user asked to review first.** A standing rule does not
  override a specific instruction.
- **Anything the task itself flagged as uncertain.** If you wrote "I'm not
  sure this is right" in your own PR description, merging answers your own
  open question.
- **A PR against an alpha or beta branch.** Those are release-stage merges
  and stay the user's call.
- **Anything genuinely destructive** -- history rewrites, force pushes,
  deleting branches holding unmerged work.

→ [Why merging is yours, why polling is load-bearing, why `--rebase`](docs/agent-notes.md#merging-your-own-pr)

### Writing the PR

The **title lands in the release notes verbatim** -- write it to commit-subject
standard, prefix included; for a single-commit branch reuse that subject.

The description must carry **the "why", not just the what** (the diff says
what changed) and **`Closes #NN`** when it resolves a tracked issue.

**Label the PR** with its `type:` and `area:` labels -- `.github/release.yml`
categorizes by label, not by the title prefix, so an unlabelled PR lands under
"Other changes".

`--fill` takes the description from your commit bodies, which is usually
right. Write it explicitly when the commits individually undersell the branch.

## Branch naming

Named for what they do, prefixed by the Conventional Commits type the
branch's commits use:

```
feat/recent-projects-header
fix/99-sidecar-lineage-walk
docs/release-methodology
```

Include the issue number when there is one. `claude/*` names still work but
say nothing about the change.

## Commit messages are Conventional Commits

```
<type>(<scope>): <subject>
```

`feat` and `fix` reach users and therefore the changelog. `docs`, `test`,
`refactor`, `chore`, `style` are filtered *out* of user-facing notes -- so a
user-visible change committed as `chore` silently disappears from them. A
breaking change needs `!` (`feat(api)!: ...`) or a `BREAKING CHANGE:` trailer.

**Writing the subject** -- for someone reading a changelog, not the diff:

- **Imperative mood**: `add`, `drop`, `hide`, `record`, `reject`.
- **Lowercase after the colon**, no trailing period.
- **~65 characters**, hard-stop ~72. Longer only when the extra words carry
  real information.
- **Use a scope when one is obvious**, reusing an existing one: `frontend`,
  `ui`, `api`, `queue`, `pipelines`, `models`, `services`, `agent`,
  `provenance`, `timing`, `icons`, `ops`, `launcher`. No scope is fine for
  genuinely cross-cutting work; don't invent a near-synonym for one in use.
- **Say what changed for the user**, and where it clarifies, contrast it with
  the old behaviour ("..., not per refetch").

→ [Worked examples, the table of what to avoid](docs/agent-notes.md#conventional-commits-and-the-changelog)

### Hooks: install them once per checkout

```bash
git config core.hooksPath ops/hooks
```

This installs `commit-msg` (the same Conventional Commits regex CI's
`commit-check.yml` runs) and `pre-commit` (ruff).

**Ruff is not a CI check** -- a PR can no longer go red on style. It still
runs at commit time because the run that reports `E501` also reports `F821`
and syntax errors:

```bash
ruff check --config backend/pyproject.toml backend/app backend/tests ops e2e
```

Most findings fix themselves with `--fix`. The hook lints the whole tree, not
just staged files, and passes if ruff isn't on `PATH`. **`--no-verify`
bypasses it and nothing downstream will catch what you skip** -- a wrapped
line is fine to skip; an `F821` is a bug with no other gate.

→ [Why CI stopped linting, why `--config` is explicit, hooks in worktrees](docs/agent-notes.md#ruff-runs-locally-not-in-ci)

## Fix every error you find, including pre-existing ones

**When a lint or test run reports errors, fix all of them** -- not only the
ones your diff caused. Don't separate "mine" from "pre-existing", don't file
the rest for someone else, don't report a red check as somebody else's
problem. If you found it, it is yours.

- **Lint the whole tree the way the hook does**, from the repo root with
  `--config backend/pyproject.toml`. A staged-files-only check goes green
  while a file the commit didn't touch is broken.
- **`--fix` is a starting point, not the fix.** Read what it changed, then
  run the tests for every file it touched.

The same applies to a failing test you didn't write. If a fix is genuinely
out of scope -- it needs a design decision, or would balloon the diff past
reviewability -- that is the one case for filing an issue, and it still
means saying so explicitly in the PR.

→ [The `SidecarRole` bug this rule exists for](docs/agent-notes.md#fixing-errors-you-did-not-cause)

## Filing out-of-scope issues you find along the way

If you notice a problem outside the current ticket that doesn't block you --
dead code, a stale doc, a missing test, a bug elsewhere -- file it as a
GitHub issue yourself. Don't ask first; this is pre-authorized. Give it a
clear title and description and the same `type:`/`area:`/`status:`/
`priority:`/`difficulty:` labels, then keep working on the original task.

If the problem *does* block you, that's not this case: fix it, or stop and
explain why you can't.

## Update the issue as work progresses

Update the tracked GitHub issue at each significant step -- spec done,
implementation done, task done. `status:specification document` means the
spec needs writing, `status: implementation plan` means the plan does,
`status: ready` means it's ready to implement.

## Running the app: one instance, not dev/prod

`docker compose up` is the only way this app runs, and Compose auto-loads
`docker-compose.override.yml` -- that override *is* how the app runs, not an
occasional extra.

**Port 5173 is the one instance.** There is no second production instance to
check. To see code changes:

```bash
docker compose up -d --build api web worker
```

Do not stand up a second copy to compare dev against prod, treat the `prod`
target as something to keep in sync, or ask whether to verify against "the
production build".

**`--build` is what ties the running stack to the checkout.** The base file
names registry tags, so anything bypassing the override (`-f
docker-compose.yml` alone, `docker compose pull`) runs the last *published*
build and serves stale code with nothing saying so. Don't switch to `pull`,
and don't add `-f` flags -- the override loads on its own.

**`worker` does not hot-reload.** After changing a queue handler
(`app/queue/pipeline_handlers.py` or anything it imports):

```bash
docker compose restart worker
```

Otherwise the job appears to run with your fix while executing the old
in-memory code.

→ [Why the override is load-bearing, why stale images are silent](docs/agent-notes.md#one-running-instance-not-devprod)

### Worktrees get their own stack

**Never run plain `docker compose` from a worktree** -- it silently repoints
*the* 5173 stack at that worktree's source. A `PreToolUse` hook blocks it;
naming a project explicitly (`-p`, `COMPOSE_PROJECT_NAME=`) passes through.

To check what the stack is serving:

```bash
docker inspect biopipe-worker-1 --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}'
```

If that path is a worktree, re-run the rebuild from the main repo root.

To exercise a worktree's code:

```bash
./ops/worktree-up.sh             # UI on 5273, API on 8100
./ops/worktree-up.sh --down      # stop it, delete its volumes
```

**A stack you brought up is yours to bring down.** This applies to what *you*
started for verification -- not the shared 5173 stack, which stays up.
`docker stop` is enough and non-destructive; `--down` also deletes that
stack's volumes.

`--down` only works from inside the owning worktree, which is why orphans
accumulate. From anywhere:

```bash
./ops/worktree-up.sh --list              # every stack, and whether its worktree exists
./ops/worktree-up.sh --prune --dry-run   # what would be removed
./ops/worktree-up.sh --prune             # tear down orphans
```

→ [Why the project name traps you, what leftover stacks cost](docs/agent-notes.md#worktrees-and-the-compose-project-name)

## Verifying changes

**UI changes:** manual testing at localhost:5173 (5273 from a worktree) --
there is no jsdom/testing-library setup. Pure-function component tests that
call component functions directly under Vitest are an established pattern
(`AlignerParamFields.test.tsx`, `ExpressionCharts.test.tsx`).

**Backend changes:** `pytest`, inside the `api` container, from the **main
repo root**:

```bash
make test
```

Runs in parallel (8 workers, then a sequential phase for `heavy`-marked
tests): ~40s against ~147s serially. Every worker gets its own database, so
this is safe alongside another agent's suite. `make test-serial` is the
escape hatch when interleaved output makes a failure hard to read;
`PYTEST_WORKERS=N` changes the width.

**Call `pytest` directly, never `python -m pytest`** -- `python` and
`python3` both resolve to a tool venv with none of the app's dependencies.
Use `/usr/local/bin/python3.12` when you need an interpreter.

**From a worktree, `docker compose exec api` silently tests *main's* code.**
Use:

```bash
./backend/run-worktree-tests.sh tests/ -q
```

It parallelizes the same way; passing your own `-n`, `-m` or `--dist` opts
out and runs exactly what you asked. It mounts the worktree's source and
starts a private Mongo replica set.

**Check a rule against the real database, not only its unit tests.** A quick
`docker compose exec api python3.12 -c "..."` against real objects is worth
more than another fixture.

→ [Worker-count measurements, the isolation design, the traps](docs/agent-notes.md#test-isolation-and-parallelism)

## Writing the specification

A well-written requirement states one thing someone can check:

- **Testable** — you can describe how to demonstrate it is met. This is the
  gate that catches everything else: if you cannot, it is a preference.
- **Unambiguous** — two people build the same thing from it.
- **Necessary** — delete it and something a user needs stops working.
- **Feasible**, **Complete** (doesn't rely on a fact only in someone's head),
  **Consistent** (doesn't contradict another requirement).

Practical rules:

1. **One obligation per statement.** A sentence with "and" is two
   requirements if the halves can pass and fail independently.
2. **Name the actor.** "Reports must be approved" → "A finance manager must
   approve a report before it can be published."
3. **Specify the what, not the how.** "A user uploading a file larger than
   5 MB can tell the upload is progressing" is a requirement; "show a
   progress bar with percentage" is a design decision disguised as one.
   Unless a regulation, contract, or design system mandates the how.
4. **Give every requirement a permanent identifier**, never reused -- reused
   IDs make old review comments lie.
5. **Check the set for contradictions.** Two well-written requirements can be
   impossible together.
6. **Record the source and the decision** -- who asked, what problem it
   serves, what you chose where there was a choice.

Non-functional requirements are where most teams are weakest:

| Category | What to specify |
|---|---|
| **Performance** | An operation, a target, a load, and a percentile. |
| **Availability** | Uptime target, measurement window, what happens to in-flight work during failure. |
| **Security** | Authentication, authorisation, data at rest and in transit, retention and deletion. |
| **Accessibility** | A named conformance level (e.g. WCAG 2.2 AA), not "should be accessible". |
| **Capacity** | Expected volumes, growth rate, maximum sizes, behaviour at the limit. |

Deeper treatment:
[projan.ai/blog/what-good-requirements-look-like-and-how-to-write-them](https://projan.ai/blog/what-good-requirements-look-like-and-how-to-write-them).

## Release methodology

Four stages. The diagrams in `assets/` are the reference:
`BioFlowReleasePath.svg`, `BioFlowReleaseLifecycle.svg`,
`BioFlowReleaseSemantics.svg`.

| Stage | Branch | Tag | What happens |
|---|---|---|---|
| Dev | `main` | none | Feature and fix PRs merge here |
| Alpha | `alpha/X.Y.Z` | `vX.Y.Z-alpha` | Cut when a release is wanted; rigorous testing |
| Beta | `beta/X.Y.Z` | `vX.Y.Z-beta` | Cut when alpha stabilizes; broader testing |
| Production | `release/X.Y.Z` | `vX.Y.Z` | Cut when ready to ship; images and launchers built |

The rules easy to get backwards:

- **Fixes found in alpha go in by PR *into the alpha branch*, then merge back
  to `main`.** Not the other way around. Same for beta.
- **Beta takes bug fixes only.** A feature discovered missing during beta
  waits for the next version; adding it invalidates the testing beta exists
  to do.
- **Nothing is cherry-picked forward** from `main` into an existing alpha or
  beta. Alpha is cut *from* `main` once; after that the flow is alpha → main.

### An issue fixed on a release branch stays open until the backflow

`Closes #NN` only auto-closes on the default branch. That is GitHub's
behaviour and it is not configurable -- and it is the behaviour you want,
because an alpha or beta can be recut or abandoned, so a fix landing there is
not delivered yet.

What that used to cost was visibility: the fix was written, the issue looked
untouched, and nothing on it said otherwise. So the state is carried by a
label instead, and it is fully automatic:

- **Merging a PR into `alpha/**`, `beta/**` or `release/**`** makes
  `release-branch-fixes.yml` read `Closes #NN` from the *PR body*, add
  **`status:fixed-in-release-branch`** to each issue, and comment with the
  branch and PR. Nothing changes about how you write the PR.
- **Merging the backflow PR into `main`** closes those issues natively.
- **Closing any issue** strips the label. It means "fixed, awaiting
  backflow" and nothing else, so the closed state replaces it.

**The backflow PR's body must carry every `Closes #NN`** -- that is the only
thing that actually closes anything, and a body assembled by hand is the step
that gets forgotten. Generate it:

```bash
ops/backflow-pr-body.sh beta/0.6.0
```

It reads the merged PRs' bodies -- the same source the workflow labels from,
so a labelled issue and a closed issue cannot drift apart -- and omits any
issue already closed (`--all` includes them).

Which number to bump:

- **Major** (`X.0.0`) -- platform-level; allowed to break features, config,
  the API, or MCP tools.
- **Minor** (`1.X.0`) -- new features, backwards compatible.
- **Patch** (`1.0.X`) -- bug fixes, typos, unexpected behaviour. No new
  features.

Cutting any of these is the user's call, not something to do because a task
finished. See [VERSION.md](VERSION.md) for the mechanics. One release covers
both the app and the launcher since
[#335](https://github.com/syntheticgio/bioflow/issues/335).

## Release notes come from PR titles

Two generators, two inputs, deliberately different:

- **GitHub release body** -- merged PR titles, categorized by PR *label* via
  `.github/release.yml`.
- **`CHANGELOG.md`** -- `feat`/`fix` subjects and their bodies, via git-cliff
  inside `ops/release.sh`. The only place commit *bodies* are read.

The thing you control is the input: a well-typed subject and a PR title and
description that explain the why. A `chore:`-typed feature, or a `--fill`ed
PR whose commits never said why, is data no generator can recover.

## Closing out a TODO entry

`docs/TODO.md` is the backlog, and **finishing the work is not finishing the
entry**. When work lands that resolves one, in the same commit or the next:

- **Append ` — FIXED` to its heading** with a short note: what shipped, when,
  where the code lives. Keep the original body -- the diagnosis explains why
  the code looks the way it does.
- **Say what the implementation did differently** from its plan.
- **Record measurements** if the entry claimed a number.
- **Move the whole entry to `docs/TODO-done.md`.** A partially resolved entry
  stays put, so the still-open part isn't buried.

**"PR merged to `main` and tested to the best of your ability" is the bar** --
don't hold an entry open waiting for the user's later testing. The bar is the
*merge*, not the PR. If testing after merge finds a real problem, that's a
new entry.

Delete an entry only when it was wrong to begin with, not merely done.

→ [What stale entries cost, why plan checkboxes lie](docs/agent-notes.md#closing-out-a-todo-entry)

## Hand-maintained registries keyed by an enum

A module-level dict keyed by something an enum enumerates, where a missing
member is silently skipped rather than raised. **Before adding a case, work
out which of three kinds it is** -- forcing the middle one into the first
one's pattern is not more correct, it is a detector that starts guessing:

- **Genuinely derivable** — cover it exhaustively, with a companion
  `frozenset` for deliberate omissions and a `set(TheEnum) == set(dict) |
  companion` test. **This is the pattern to copy.**
- **Intentionally partial** — an open vocabulary where "no answer" is a valid
  result. Needs a written inclusion rule and a *reachability* test in the
  other direction, not forced coverage.
- **Keys owned outside this repo** — a bad key fails loudly elsewhere, so the
  risk is internal: parallel structures that can drift apart silently.

**A registry pair -- "classified" and "not double-classified" -- must be run
together.** A fix that adds an entry can collide with a fix that excludes it,
and only the partition test catches it. Run the whole `TestExhaustiveness`
class, not the one test a bug report names.

→ [The STAR failure, the three-way audit, the #355 collision](docs/agent-notes.md#hand-maintained-registries-keyed-by-an-enum)

## Adding a pipeline tool

Registering in `backend/app/pipelines/tools.py` is **half** the change:

1. **`suggestion_service.py`** decides which tool each Actions-tab card
   recommends and is hand-maintained. Check for a rule that should now pick
   it, or a card whose `unavailable` reason just stopped being true. Add the
   case to `backend/tests/services/test_suggestion_service.py`.
2. **`TOOL_META`** backs `/help/software` and requires `homepage`,
   `citation`, `license` and `usage`. Verify license and citation against the
   project's own repository rather than recalling them --
   `repository`/`citation_url` are deliberately optional so a tool with no
   public repo leaves them blank rather than inviting a fabricated value.
   Write `usage` as behaviour, not flags. Same for
   `backend/app/pipelines/sources.py`, which backs `/help/sources`.

A tool that gets its own **job type** has four more hand-maintained
registries, none of which fail at import -- each one goes wrong silently, in
a different place:

3. **`tools.all_tools()`** -- the probe list behind the availability panel. A
   probe defined but not listed here reports nothing to the UI.
4. **`node_types.NODE_TYPES`** -- needs a `_launch_*` adapter *and* a
   `NodeTypeSpec`. This one does fail a test (`launch_function_names()` is
   discovered by inspection), which is the exception that proves the rule.
5. **`running_now.ENDPOINT_JOB_TYPES`** -- maps the card's endpoint to its job
   type. Missing, the card's Launch button never greys out while the job runs.
6. **`provenance_walker._NO_NARRATIVE_STEP`** (or a narrative verb) -- every
   registered handler must be classified one way or the other.

Frontend, if the tool's results get a panel: **`metricInfo.METRIC_INFO`** needs
an entry per `<Stat metric="...">`, enforced by `metricInfo.test.ts`. Without
one the InfoMarker silently renders nothing.

**Testing availability:** patch `spec_for`, not `tools.<name>` (the registry
captured the function object at import time). And assert the card flips to
*unavailable* when the probe is patched off -- the image ships most tools, so
an "available" assertion passes whether or not the patch worked.

**Installing it:** check apt against a real container *with a control package
in the same run* before believing "not packaged" or "packaged" -- and prefer
bioconda to a GitHub release binary when upstream ships x86-64 only, since
bioconda usually has a linux-aarch64 build and that is the difference between
the tool working on Apple Silicon and an arm64 skip. This image ships without
`curl` (install-meryl.sh and install-quast.sh purge it), so a late install
script must reinstall and re-purge it. End the script with a real run, not
`--version`: a tool that dlopens its libraries passes `--version` with its
libraries deleted.

→ [The silent Flye failure, the `runnable` comment that lied](docs/agent-notes.md#adding-a-pipeline-tool)

## Adding an AI-using feature

AI calls go through `app/services/ai/`, never directly to an HTTP endpoint:

```python
provider = await ai.resolve(TaskSlot.YOUR_SLOT)   # None means nothing configured
result = await ai.complete(provider, system=..., user=...)
```

- **A new feature needs a new `TaskSlot`** in `app/models/ai.py` plus a label
  in `_SLOT_LABELS`. Reusing an existing slot silently ties two features to
  one provider with no way to separate them.
- **`complete()` never raises and never returns None.** Check
  `isinstance(result, Completion)` -- `if result is None` type-checks, reads
  as correct, and treats every failure as a success.
- **Thread handlers must not call `asyncio.run()`.** Use
  `app.db.client.run_from_thread` and `complete_sync()`.

→ [Why a second event loop breaks Motor](docs/agent-notes.md#ai-features)

## Querying computation records

`job_timings` holds one row per completed job and **includes failed runs**.
Provenance and OOM detection want them; the predictive models must never see
them.

**Do not query the collection directly to fit or summarize anything.** Use
`timing_service._modelled()` (or a caller) for models, and
`records_for_object()` for provenance.

Resource fields are null for runs under 60 seconds -- a null peak is a short
job, not failed sampling.

→ [Why an OOM-killed run poisons the estimates](docs/agent-notes.md#querying-computation-records)
