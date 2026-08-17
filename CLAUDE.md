# Working on this repo

Single-user, local-only tool for non-critical, non-essential work. Optimize for
"the person using it can see their change" over correctness-at-scale practices
that make sense for a team or a hosted service.

**`main` is this project's dev trunk, not its production branch.** Shipping to
users happens at a tag, from a release branch, several stages downstream of
`main` (see [Release methodology](#release-methodology) below). Landing on
`main` is not shipping.

**`main` is reached through a pull request, not a direct merge** -- but you
open *and* merge that PR yourself once CI is green, per the section below. The
PR is the record and the CI gate, not a wait for someone else. The rest of the
pipeline -- alpha, beta, production -- is described under
[Release methodology](#release-methodology); this section is only about how a
piece of work gets from a worktree onto `main`.

## Finish work on a branch, push it, open a PR, and merge it once CI is green

**Merge your own work to `main` once every check passes.** The end state of a
task is *a merged PR*. The PR still exists -- it is what generates the release
notes and what someone reads later to understand the change -- but it is not a
gate you wait at. Committing, pushing, opening the PR, and merging it are all
yours to do without asking.

This changed on 2026-08-17, reverting the 2026-08-09 rule that made merging
the user's step. The reason is that nothing ships at a merge to `main`: it is a
dev trunk, and alpha, beta, and production are all downstream of it (see
[Release methodology](#release-methodology)). A green PR sitting open is not
being reviewed, it is just waiting -- and the cost of that wait is real, since
`main` moves underneath it and the next task starts from a base that does not
include it.

**Green CI is the whole gate, so the checks have to have actually finished.**
"Green" means every check reports `pass` -- not `pending`, not "the ones I
looked at." The `gh pr checks` polling described below is what establishes
that, and it is now load-bearing rather than merely diligent: it is the only
thing standing between a bad change and `main`.

What still earns a stop-and-ask rather than a merge:

- **A red check**, obviously -- fix it and re-poll, don't merge around it.
- **A merge conflict** (`mergeStateStatus: DIRTY`). Rebase and push; that is
  ordinary work on your own branch.
- **A change the user asked to review before it lands.** A standing rule does
  not override a specific instruction on a specific task.
- **Anything the task itself flagged as uncertain.** If you wrote "I'm not sure
  this is the right approach" in your own PR description, merging it is
  answering your own open question. Say so and leave it.
- **A branch whose PR is against something other than `main`** -- an alpha or
  beta branch. Those are release-stage merges and stay the user's call, per
  [Release methodology](#release-methodology).

**Before opening the PR, catch up to `main` yourself rather than letting
GitHub discover the conflict.** `main` moves while a task is in progress, and
a PR opened against a stale base either merges something it was never tested
against or sits there reporting `mergeStateStatus: DIRTY` until someone
notices. Doing this before `gh pr create` rather than after means the PR is
mergeable the moment it exists, not eventually:

```bash
git fetch origin main
git rebase origin/main
```

Rebase is the default because it keeps history linear and each commit's
subject reviewable on its own, matching how this repo already handles a
conflict discovered *after* the PR is open (see below). If the rebase itself
conflicts in a way that isn't a quick per-commit fix -- large divergence,
conflicts repeating across several commits -- fall back to a merge instead of
fighting it commit-by-commit:

```bash
git rebase --abort
git merge origin/main
```

Either way, resolve conflicts the same way you would resolve them mid-task:
read both sides, keep what's correct, don't take a side blindly because it's
"theirs" or "ours."

**Then verify your changes actually survived.** A rebase or merge can resolve
a conflict by silently dropping a hunk if a resolution was accepted too
quickly. Before pushing, confirm the diff against `origin/main` still
contains the work the task set out to do:

```bash
git diff origin/main...HEAD --stat
```

Check the file list matches what you intended to touch, and skim the diff
itself for anything that looks reverted or missing -- not just that the
command ran without error.

Once the suite is green:

```bash
git push -u origin HEAD
```

```bash
gh pr create --base main --fill
```

**Opening the PR is not the end of the task -- watch CI and fix what it
finds.** `gh pr create` returns before any check has run. Poll
`gh pr checks <N>` (and `gh pr view <N> --json mergeable,mergeStateStatus`
for conflicts) until every check reports pass/fail, not just until the
command returns -- a "pending" read seconds after creation is the run not
having started yet, not a signal to stop watching. This caught a real bug on
#217/#314: the local suite was green, but CI's `ruff check` failed on an
import-order rule (`I001`) the local run never invoked, and the fix was a
one-line split of a combined import. Read the job log, apply the minimal fix
ruff itself suggests (or the equivalent for whatever check failed), push, and
re-poll -- don't leave a red check for the user to notice and report back to
you. Same for `mergeStateStatus`: if it comes back `UNSTABLE` (checks still
running) that's fine and you keep waiting, but if it's a real conflict,
rebase your branch on `origin/main` and push again -- that is ordinary work
on your own branch, not a history rewrite of a shared one.

Once every check reports `pass` and `mergeable` is `MERGEABLE`, merge it:

```bash
gh pr merge <N> --rebase --delete-branch
```

**`--rebase`, not `--squash`.** This matters more than it looks. `main`'s
history is one commit per unit of work, first-parent -- check `git log
origin/main --first-parent` and you will see the individual subjects, not merge
bubbles or squashed PR titles. Two things depend on that:

- **`CHANGELOG.md` is generated from commit subjects *and bodies*** by git-cliff
  inside `ops/release.sh` (see
  [Release notes](#release-notes-come-from-pr-titles)). A squash concatenates
  the bodies into one blob under a single subject, which is precisely the input
  that generator cannot use.
- **The separable-commits rule below assumes the commits survive.** Splitting a
  mechanical rename from a behaviour change is pointless if the merge glues
  them back together.

`--delete-branch` because a merged branch left on the remote is what
`worktree-up.sh --prune` later has to reason about. Note it deletes the remote
branch, not your worktree -- tear that down separately, as described under
[Running the app](#running-the-app-one-instance-not-devprod).

**If the task ran in a worktree, remove it once the merge lands.** A merged
PR is the same "done with this" signal that governs bringing down a test
stack, below -- the branch is gone from the remote, so there is nothing left
for the worktree to hold open. Bring down anything you brought up for testing
in it first (`./ops/worktree-up.sh --down`), then remove the worktree itself
rather than leaving it for the end-of-session prompt: `ExitWorktree` with
`action: "remove"` if the harness put you there via `EnterWorktree`, or `git
worktree remove <path>` from the main checkout otherwise. Skipping this is
what `worktree-up.sh --prune` exists to clean up after the fact, but that is
a machine-wide sweep for orphans nobody remembered, not a substitute for
removing your own when the task that needed it is finished.

Then report the merge and the PR URL, and stop.

Do not use `--auto`. It queues the merge for whenever checks pass and returns
immediately, which means the task ends with you having verified nothing -- the
point of polling is that *you* saw the checks pass before anything landed.

What still earns a pause before you push or merge:

- **A red or unrun suite.** "Green" is the precondition, and it means read the
  count, not the exit code of whatever was last in the pipeline.
- **Anything genuinely destructive** -- history rewrites, force pushes,
  deleting branches that hold unmerged work. Committing and merging a green PR
  are cheap and revertable; those are not.

Keep commits separable: a mechanical rename and a behaviour change in one
commit is a commit nobody can review or revert, whatever the test count says.
Self-merging raises the stakes here rather than lowering them. Nobody is
reading the diff before it lands, so a separable commit is what makes the
change reviewable *after* the fact -- and the ability to revert one commit
without unpicking three is the safety net that replaces the review gate. The
commits are also read directly by git-cliff at release time, per `--rebase`
above.

### Writing the PR

**The PR title is what lands in the release notes verbatim** -- see
[Release notes](#release-notes-come-from-pr-titles). Write it to the same
standard as a commit subject, and for a single-commit branch just reuse that
subject. Everything under
[Writing the subject line](#writing-the-subject-line) applies to it,
Conventional Commits prefix included.

The description is the unit the user actually reviews. Two things it must
carry:

- **The "why", not just the "what".** The diff already says what changed.
- **`Closes #NN`** when the work resolves a tracked issue, so the issue closes
  on merge rather than being closed by hand later.

**Label the PR** with its `type:` and `area:` labels. `.github/release.yml`
categorizes the notes by label, not by the title's `feat:`/`fix:` prefix, so
an unlabelled PR lands under "Other changes".

`--fill` takes the description from your commit bodies, which in this repo are
already written at the right level of detail. That is usually the right call;
write the body explicitly when the branch's commits individually undersell
what the branch does as a whole.

## Writing the specification

The design-spec step in the workflow produces requirements that an implementer
(agent or human) can build from without guessing. A well-written requirement
states one thing that someone can check. Use these six quality criteria on
every sentence:

- **Testable** — you can describe how to demonstrate it is met. This is the
  gate that catches everything else: if you cannot, it is a preference, not a
  requirement.
- **Unambiguous** — two people reading it separately build the same thing.
- **Necessary** — delete it and something a user needs stops working.
- **Feasible** — it can be built inside the constraints you have.
- **Complete** — it does not rely on a fact that exists only in someone's head.
- **Consistent** — it does not contradict another requirement in the same set.

Practical rules that follow from these:

1. **One obligation per statement.** If a sentence contains "and", split it —
   two things that can pass and fail independently are two requirements.
2. **Name the actor.** "Reports must be approved" → "A finance manager must
   approve a report before it can be published." Anonymous requirements get
   built for a user nobody has met.
3. **Specify the what, not the how.** "A user uploading a file larger than 5 MB
   can tell that the upload is progressing" is a requirement. "Show a progress
   bar with percentage complete" is a design decision disguised as one. State
   the constraint and leave the solution to the person who knows the system
   best, unless a regulation, contract, or design system mandates the how.
4. **Give every requirement a permanent identifier** and never reuse it, even
   after deletion. Reused IDs make old review comments lie.
5. **Check the set for contradictions.** Two well-written requirements can be
   impossible together (e.g. "records retained 7 years" and "user data removed
   within 30 days of account closure").
6. **Record the source and the decision.** Who asked, what problem it serves,
   and what you chose where there was a choice. That is what lets the document
   survive its author leaving.

Non-functional requirements are where most teams are weakest — they surface in
the last week when a load test fails or a security questionnaire arrives. Five
categories cover most of what goes missing:

| Category | What to specify |
|---|---|
| **Performance** | An operation, a target, a load, and a percentile. |
| **Availability** | Uptime target, measurement window, and what happens to in-flight work during failure. |
| **Security** | Authentication, authorisation rules, data at rest and in transit, retention and deletion. |
| **Accessibility** | A named conformance level (e.g. WCAG 2.2 AA), not "should be accessible". |
| **Capacity** | Expected volumes, growth rate, maximum file/record sizes, behaviour at the limit. |

For a deeper treatment, see
[projan.ai/blog/what-good-requirements-look-like-and-how-to-write-them](https://projan.ai/blog/what-good-requirements-look-like-and-how-to-write-them).

## Branch naming

Branches are named for what they do, prefixed by type:

```
feat/recent-projects-header
fix/99-sidecar-lineage-walk
docs/release-methodology
```

The type prefix matches the Conventional Commits type the branch's commits
use (`feat`, `fix`, `docs`, `refactor`, `test`, `chore`). Include the issue
number when there is one. Agent-generated `claude/*` branch names still work
and nothing rejects them, but they say nothing about the change, and they now
show up in a PR list that someone reads.

## Commit messages are Conventional Commits

This is not new -- roughly 98% of the last 300 commits already follow it --
but it is now load-bearing rather than merely tidy, because the changelog is
generated from it:

```
<type>(<scope>): <subject>
```

`feat` and `fix` are the types that reach users and therefore the changelog.
`docs`, `test`, `refactor`, `chore`, `style` are real and correct types that
are filtered *out* of user-facing notes. Two consequences:

- **A user-visible change committed as `chore` disappears from the release
  notes.** Nothing errors; the entry is simply absent.
- **A breaking change needs `!`** -- `feat(api)!: ...` -- or a
  `BREAKING CHANGE:` trailer in the body. That marker is what a major-version
  bump and a "breaking" changelog section are keyed off, and it cannot be
  recovered later from the diff.

Use `tweak` and `style` sparingly; both appear in the history and neither is
a standard Conventional Commits type.

### Catching a bad subject before it reaches CI

`commit-check.yml` rejects a PR whose commit subjects don't parse as
Conventional Commits, and the easiest way to hit that check is to not have
written the commit yet when you find out. `ops/hooks/commit-msg` runs the
same regex locally, at `git commit` time, and prints the same fix-it message
CI does. It is not installed by default -- git does not auto-run hooks from a
clone -- so opt in once per checkout (main checkout or worktree alike):

```bash
git config core.hooksPath ops/hooks
```

If a commit is rejected, the message names the exact problem; usually the
fix is `git commit --amend` with a corrected subject, or (mid-rebase)
`reword`ing the offending commit. This is the same fix CI's own error output
walks through, so there is no second thing to learn.

### Writing the subject line

**Write the subject for someone reading a release changelog, not for someone
reading the diff.** It is the one line that survives into the notes, and by
then the code is not in front of the reader. This is the single highest-value
thing to get right in a commit.

The mechanics, all of which this repo's history already follows -- match it
rather than introducing a second style:

- **Imperative mood**, as if completing "this commit will ...": `add`, `drop`,
  `hide`, `record`, `reject`. Not `added`, `adds`, or `adding`.
- **Lowercase after the colon** (11 of the last 400 commits capitalize; don't
  add to them) and **no trailing period** (zero do).
- **Aim for ~65 characters, hard-stop around 72.** That is this repo's median.
  Longer is allowed when the extra words carry real information -- several of
  the best subjects below run past it -- but a long subject is usually a sign
  that detail belongs in the body.
- **Use a scope when one is obvious**, and reuse an existing one:
  `frontend`, `ui`, `api`, `queue`, `pipelines`, `models`, `services`,
  `agent`, `provenance`, `timing`, `icons`, `ops`, `launcher`. About a quarter
  of commits have no scope, which is fine when the change is genuinely
  cross-cutting. Don't invent a near-synonym for a scope already in use.

**Say what changed for the user, not what you edited.** The diff already
records which functions moved. The subject is the only place the *behavior*
gets stated:

```
fix(frontend): record a project visit once per navigation, not per refetch
fix(queue): advance workflow nodes on every terminal path, not just complete()
feat(activity): say what each run is doing, not just what it acted on
fix(frontend): hide RECENT block entirely when zero chips fit
feat(ui): quality badge as a serif figure instead of a color dot
```

Those are real subjects from this repo, and they share a shape worth copying:
**they name the new behavior and, where it clarifies, contrast it with the old
one** ("..., not per refetch"). A reader who never sees the diff still learns
what changed. That contrast is also what makes a changelog entry useful rather
than merely present.

What to avoid, and why each one fails:

| Avoid | Why |
|---|---|
| `fix: bug fix` / `fix(ui): fix issue` | Says nothing the type didn't already say. |
| `feat: update ProjectExplorer.tsx` | Names a file, not a change. The diff has the filename. |
| `fix: address PR feedback` | Meaningless outside the PR; unreadable in a changelog six months on. |
| `chore: various improvements` | Bundles unrelated work *and* hides it from the notes. |
| `feat: WIP` | A commit that admits it isn't a unit of work. |

If you cannot write a clear subject, that is usually the commit telling you it
does two things and should be two commits -- see the note above about keeping
commits separable.

## Release methodology

BioFlow releases through four stages. The diagrams in `assets/` are the
reference: `BioFlowReleasePath.svg` (the stages), `BioFlowReleaseLifecycle.svg`
(the branching topology), `BioFlowReleaseSemantics.svg` (what each number
means).

| Stage | Branch | Tag | What happens |
|---|---|---|---|
| Dev | `main` | none | Feature and fix PRs merge here |
| Alpha | `alpha/X.Y.Z` | `vX.Y.Z-alpha` | Cut when a release is wanted; rigorous testing |
| Beta | `beta/X.Y.Z` | `vX.Y.Z-beta` | Cut when alpha stabilizes; broader testing |
| Production | `release/X.Y.Z` | `vX.Y.Z` | Cut when ready to ship; images and launchers built |

The rule that matters for an agent, because it is the one that is easy to get
backwards:

- **Fixes found in alpha go in by PR *into the alpha branch*, then merge back
  to `main`.** Not the other way around. Same for beta.
- **Beta takes bug fixes only, no features.** A feature discovered to be
  missing during beta waits for the next version; adding it silently
  invalidates the testing beta exists to do.
- **Nothing is cherry-picked forward from `main` into an existing alpha or
  beta.** Alpha is cut *from* `main` once; after that the flow is alpha → main.

Cutting any of these is the user's call, not something to do because a task
finished. See [VERSION.md](VERSION.md) for the mechanics.

Which number to bump, from `BioFlowReleaseSemantics.svg`:

- **Major** (`X.0.0`) -- platform-level additions; allowed to break existing
  features, server configuration, the API, or MCP tools.
- **Minor** (`1.X.0`) -- new features, backwards compatible. Judgement call on
  scope.
- **Patch** (`1.0.X`) -- bug fixes, typos, unexpected behaviour. 100%
  compatible, no new features.

**One release covers both the app and the launcher** since
[#335](https://github.com/syntheticgio/bioflow/issues/335). `make release
VERSION=X.Y.Z` bumps and publishes both; the launcher rides along even when
nothing in it changed. `make release-launcher` still exists for a launcher-only
fix, constrained to production versions above the current `VERSION`. See
[VERSION.md](VERSION.md).

`ops/release.sh` now accepts `-alpha` and `-beta` pre-release suffixes and
cuts onto the appropriate stage branches (`alpha/X.Y.Z` / `beta/X.Y.Z` /
`release/X.Y.Z`). See [VERSION.md](VERSION.md) for the cut commands and stage
table.

## Release notes come from PR titles

Release notes are generated by GitHub from the PRs merged since the previous
tag, which is why the PR title matters as much as the commit subjects
underneath it. Write it the way it should read in a changelog.

Two generators, two inputs, deliberately different:

- **GitHub release body** -- merged PR titles, categorized by PR label via
  `.github/release.yml`. Assembled by `release.yml`'s `release` job.
- **`CHANGELOG.md`** -- the `feat`/`fix` subjects and their bodies, generated
  from commit history by git-cliff inside `ops/release.sh` at cut time
  ([#106](https://github.com/syntheticgio/bioflow/issues/106)). The changelog
  is the only place commit *bodies* are read; GitHub's generator cannot see
  them.

The thing an agent controls either way is the *input*: a well-typed commit
subject and a PR title and description that explain the why. A `chore:`-typed
feature or a `--fill`ed PR whose commits never said why is data that no
generator can recover.

## Update issue when a task is completed or there is significant progress
You should update the issue that we are tracking a task with in Github with any
significant progress.  Specifically when the spec is done or the implementation is done
or the entire task is done.  The appropriate tags should be used - status:specification document
means that the spec needs to be written, status:implementation plan means
that the implementation plan needs to be written and status: ready means it
is ready to implement.  The other labels are self explanatory and should be
used when appropriate.

## Filing out-of-scope issues you find along the way

If you notice a problem that is outside the scope of the current ticket --
unrelated dead code, a stale doc, a missing test, a bug in a different area --
and it does not block finishing the current implementation, file it as a new
GitHub issue yourself. Don't ask first; this is pre-authorized.

Give the issue a clear title and description (what you saw, where, why it
matters), and apply the same labels described above (`type:`, `area:`,
`status:`, `priority:`, `difficulty:`) so it slots into the existing
triage flow. Then keep working on the original task -- filing the issue is a
side note, not a detour.

If the problem *does* block the current implementation, that's not this case:
fix it, or stop and explain why you can't, rather than filing an issue and
moving on.

## Running the app: one instance, not dev/prod

Don't build or reason about a dev vs. production split for this repo. `docker
compose up` (no extra flags) is the only way this app runs, and Compose
auto-loads `docker-compose.override.yml` on top of `docker-compose.yml` --
that override is not optional or occasional, it is *the* way this app runs.
It gives hot-reload on both sides (bind-mounted source, `uvicorn --reload`,
`vite dev`), and `docker-compose.yml`'s own `web` service target (`prod`,
static nginx build) is not what's actually deployed day to day.

**Port 5173 is the one instance.** When the user says "the running app" or
"port 5173," that's it -- there is no separate production instance to also
check, and no need to build one. If code changes need to be seen, the right
action is:

```bash
docker compose up -d --build api web worker
```

which rebuilds and restarts against current source. That's it. Do not:

- Stand up a second copy to compare "dev" against "prod" behavior
- Treat the `prod` target in `frontend/Dockerfile` or the base
  `docker-compose.yml` web service as something to keep in sync or verify
  against -- it exists but is not the deployment this user runs
- Ask whether to verify against "the production build" before believing a
  change works

If a future change happens to break the override-based setup, that's an
acceptable, fixable cost of this tradeoff -- not a reason to add process
around it now.

**After [#37](https://github.com/syntheticgio/bioflow/issues/37) lands, the
build directives move to the override file -- keep using the same command.**
`docker-compose.yml` currently declares `api`, `worker`, and `web` with
`build:` contexts. #37 converts those to `image:` references at
`ghcr.io/syntheticgio/bioflow-*`, because Compose *builds* a `build:` service
rather than pulling it, and the native launcher's users have no source tree for
it to build from. The `build:` directives move into
`docker-compose.override.yml` in the same commit, so `docker compose up -d
--build api web worker` keeps rebuilding from local source exactly as it does
today -- the override always loads, and it is the only place that still knows
how to build.

What that means for an agent working here, since agents do nearly all of the
starting and rebuilding in this repo:

- **The rebuild command does not change.** Do not switch to `docker compose
  pull`, and do not add `-f docker-compose.yml -f docker-compose.override.yml`
  -- the override loads on its own.
- **Never verify a local change against a published image.** After #37 the base
  file names a registry tag, so anything that bypasses the override (`-f
  docker-compose.yml` alone, or a `docker compose pull`) runs *the last
  published build*, not the working tree. It starts cleanly and serves stale
  code, with nothing in the output saying so. This is the same failure shape as
  the worktree-mounts trap below, and it reads the same way: "my change isn't
  in the app."
- **`--build` is what ties the running stack to the checkout.** If a rebuild
  ever seems not to take, check that the flag was actually passed before
  looking for a cause in the code.

Until #37 merges, none of the above applies and the base file still builds from
source on its own.

**`worker` does not hot-reload.** `api` runs `uvicorn --reload` and `web`
runs `vite dev`, so editing their bind-mounted source takes effect on the
next request with no restart needed. `worker` bind-mounts `./backend/app`
too but runs `python -m app.worker_main` directly with no reload mechanism,
so it keeps running whatever was loaded at process start. After a change
that affects a queue handler (`app/queue/pipeline_handlers.py` and anything
it imports), run:

```bash
docker compose restart worker
```

before re-testing a pipeline job (QC, trim, align, etc.) -- otherwise the
job appears to run with the fix but is silently still executing the old
in-memory code, which reads as "the fix didn't work" when it actually just
never got picked up.

**Plain `docker compose` always targets the one main-checkout stack on
5173/8000; testing a worktree's code goes through `./ops/worktree-up.sh`
instead (below), never through plain `docker compose` in the worktree.** The
bind mounts in `docker-compose.override.yml` are relative paths
(`./backend/app`, `./frontend/src`), so Compose resolves them against
whatever directory it was invoked from -- while the project name is pinned to
`biopipe` in `docker-compose.yml`. Running `docker compose up` inside a
worktree therefore does not create a second stack: it silently recreates
*the* stack with its source pointing at that worktree, with no error and no
warning. Port 5173 then serves that branch's code, and `docker compose
restart worker` from anywhere just reloads it again -- so a change merged to
main appears to be missing from the running app, and the handler simply never
shows up in the worker's `handlers_loaded` log line. A `PreToolUse` hook
(`ops/hooks/block-compose-in-worktree.sh`, registered in
`.claude/settings.json`) blocks bare `docker compose` from a worktree for
exactly this reason; naming a project explicitly (`-p`,
`COMPOSE_PROJECT_NAME=`) passes through, which is also what lets
`worktree-up.sh`'s own compose calls work.

To check what the stack is actually serving:

```bash
docker inspect biopipe-worker-1 --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}'
```

If that source path is a worktree instead of the main checkout, the stack is
on the wrong tree; fix it by re-running the rebuild from the main repo root
(wherever this repo is checked out on this machine -- `git rev-parse
--show-toplevel` from the main checkout, not from the worktree):

```bash
docker compose up -d --build api web worker
```

**To exercise a worktree's code before it merges, run `./ops/worktree-up.sh`
from that worktree.** It brings up a second, fully separate stack:

```bash
./ops/worktree-up.sh             # UI on 5273, API on 8100
./ops/worktree-up.sh --down      # stop it, delete its volumes
```

It works by setting `COMPOSE_PROJECT_NAME`, which outranks the `name: biopipe`
pinned in `docker-compose.yml`, so the worktree stack gets its own containers,
network, and mongo/redis volumes; `ops/docker-compose.worktree.yml` moves the
published ports. The main instance on 5173 keeps serving main throughout.

**A stack you brought up for testing is yours to bring back down when the
testing is done.** `./ops/worktree-up.sh --down` before you finish the task, in
the same way you would close a file you opened. This applies to anything *you*
started for your own verification -- a worktree stack, a throwaway container.
It explicitly does *not* apply to the shared main stack on 5173, which is meant
to stay up: leave that one running.

The cost of skipping it is not tidiness, it is other people's test runs. On
2026-08-12 a full-suite run hung twice at ~10% with mass errors, on code that
was fine; four *other* worktree stacks were still up from earlier tasks, three
with live Mongos and one with workers in a restart loop. Since `conftest.py`
drops every collection in `biopipe_test` at session start, each run was wiping
the others' data mid-test. That reads as flakiness in the code -- a rotating
handful of DB-touching tests failing, different ones each run, all passing in
isolation -- and it costs an afternoon to trace back to a container nobody
remembered starting. The same trap is described from the other direction under
[Verifying changes](#verifying-changes).

Worth knowing when you clean up: `docker stop` is enough and is not
destructive. It leaves the containers and their volumes intact, so a stack can
be brought straight back up with its database. `worktree-up.sh --down` goes
further and deletes that stack's volumes, which is the right call for a
worktree you are finished with.

**`--down` only works from inside the worktree that owns the stack**, which is
the reason orphans accumulate: a worktree gets deleted when its branch merges,
and once the directory is gone there is nowhere left to run `--down` from. Two
machine-wide subcommands cover that, and unlike everything else in this script
they run from anywhere, including the main checkout:

```bash
./ops/worktree-up.sh --list              # every worktree stack, and whether its worktree still exists
./ops/worktree-up.sh --prune --dry-run   # show which ones would be removed
./ops/worktree-up.sh --prune             # tear down the orphaned ones
```

`--prune` only removes stacks whose worktree is gone -- a stack whose worktree
still exists is somebody's live test run, and `--list` shows it without
touching it. It prints what it will remove and asks first, and declines
outright when there is no terminal to ask at. `git worktree list` is the
authority for what still exists; note that a **detached-HEAD** worktree is
matched by its directory name rather than a branch, because that is the slug
`worktree-up.sh` itself falls back to when there is no current branch.

**If the running instance ever does get pointed at a worktree, point it back
when you are done.** This is the one piece of state in this workflow that
outlives the task that changed it. `worktree-up.sh` avoids the problem by
construction, but any route that repoints the 5173 stack -- a deliberate
`COMPOSE_PROJECT_NAME=biopipe` from a worktree, a hook that got bypassed --
leaves 5173 serving a branch after the branch is finished with. Nothing
notices: the app works, it is simply not running the code anyone thinks it is,
and the next "my merged change isn't in the app" is the symptom. Restore it
from the main checkout root with

```bash
docker compose up -d --build api web worker
```

and confirm with the `docker inspect biopipe-worker-1` mount check above --
the source path should be the main checkout, not a path under
`.claude/worktrees/`.

Two things it does that are easy to get wrong by hand. It passes
`--env-file <main checkout>/.env`, because `.env` is gitignored and a worktree
has none -- without it `BIOINFO_HOME` falls back to the compose-file default
and Docker silently creates that path as an empty directory. And on first
launch it copies the main stack's `biopipe` database in with
`mongodump | mongorestore`, since a new project name means an empty database,
which would defeat the purpose of testing against real projects. That copy is
a snapshot, not a live mirror; `--reseed` refreshes it. `/data` *is* shared
with the main stack, deliberately -- fine for a UI or read-path check, worth a
thought before running a pipeline that rewrites an existing artifact.

## Verifying changes

Manual testing in the browser at localhost:5173 is the actual verification
step for anything UI-facing -- there is no headless component-testing setup
in this repo (no jsdom/testing-library, zero `.test.tsx` files) and none is
expected. From a worktree, `./ops/worktree-up.sh` serves the same UI at
localhost:5273 against that worktree's code. Backend changes are covered by
`pytest`; run it inside the `api` container (`docker compose exec api python
-m pytest tests/ -q`) rather than a bare host `.venv`, since the host venv
hits Mongo replica-set connection errors that the container's network doesn't
have.

**That `docker compose exec api` command is only correct from the main repo
root.** Run it inside a worktree and it silently tests *main's* code, not the
worktree's -- the `api` container bind-mounts the main repo's
`backend/app` and `backend/tests`, so the worktree's changes never reach the
process running the tests. Every result describes the wrong tree, and it
gives no error to say so.

From a worktree, use `backend/run-worktree-tests.sh` instead:

```bash
./backend/run-worktree-tests.sh tests/ -q
```

It starts a throwaway container that mounts the *worktree's* source on the
running stack's network, plus a throwaway single-node Mongo replica set of
its own. The private Mongo matters, not just the private source: `conftest.py`
hardcodes the test database name `biopipe_test` and drops every collection in
it at session start, so a worktree run sharing Mongo with the stack's own
`api` container (or with another worktree's test run) wipes the other's data
mid-test. That surfaces as a rotating handful of DB-touching tests failing --
different ones each run, all passing in isolation -- which reads as flakiness
in the code when it is actually two test runs fighting over one database.
Measured on one unchanged tree: 7 failed, then 1872 passed, then 5 failed,
sharing Mongo with the stack; five consecutive runs at an identical count with
a private one.

**Check a rule against the real database, not only its unit tests.** The
Actions tab's suggestion rules passed a full green suite while getting two
things wrong that one look at a real project exposed: `protein.faa` and
`cds_from_genomic.fna` were counted as alignable references because they are
FASTA, and the same assembly stored twice counted as two. Both made a project
with one usable reference refuse to align. The tests were green because they
fed the rules hand-built objects that already looked the way the rules
expected. A quick

```bash
docker compose exec api python -c "..."
```

against real objects is worth more than another fixture here.

## Closing out a TODO entry

`docs/TODO.md` is the backlog. **Finishing the work is not finishing the
entry** -- and this has already gone wrong three times.

On 2026-07-31 an audit found that three of its seven entries described work
that had shipped days earlier. All three were fixed by one plan
(`docs/superpowers/plans/2026-07-29-queue-and-role-provenance-cleanups.md`)
that ran to completion without anyone touching the backlog afterwards. The
cost is not tidiness: one stale entry advised deleting `JobContext.extend_lease`
as dead code, and by then four handlers were calling it. Acting on the TODO
would have broken working code.

So, when work lands that resolves an entry, in the same commit or the one
right after:

- **Append ` — FIXED` to its heading** and write a short note under it saying
  what shipped, when, and where the code lives. Keep the original body: the
  diagnosis explains why the code looks the way it does, and the next person
  hitting something similar needs it.
- **Say what the implementation did differently.** Every entry closed so far
  departed from its own plan somewhere. That delta is the most valuable
  sentence in the entry.
- **Record measurements if the entry claimed a number.** "6-15s" becoming
  "0.025s" is what makes the fix checkable later.
- **Move the whole entry to `docs/TODO-done.md`.** `docs/TODO.md` holds only
  open entries; a closed one moves there in full (heading, note, and original
  body intact) rather than staying in place, so the active backlog doesn't
  grow to carry every finished entry's context on every read. An entry that
  is only partially resolved (one aligner shipped, a sibling didn't; the core
  fix landed but a named follow-up didn't) stays in `docs/TODO.md` -- moving
  it would bury the still-open part.

Delete an entry outright only when it was wrong to begin with -- not merely
done. A `— FIXED` entry is a record; a deleted one is a question someone will
ask again.

**"PR merged to `main` and tested to the best of your ability" is the bar for
`— FIXED` -- don't hold an entry open waiting for the user's own later
testing to bless it.** `main` is a dev trunk here, not a release
([above](#working-on-this-repo)); nothing in this repo ships to users at
merge time -- alpha, beta, and production are all downstream of it -- so there
is no "final testing" step that a TODO entry should wait on. Note the bar is
still the *merge*, not the PR -- an entry whose PR is open but unmerged is not
yet `— FIXED`. Since you now merge your own green PRs, that usually means
closing out the entry in the same task rather than leaving it for later; the
case that still leaves it open is a PR held back for one of the reasons listed
under [merging](#finish-work-on-a-branch-push-it-open-a-pr-and-merge-it-once-ci-is-green).
If testing after merge turns up a real problem,
that is a new entry, not a reason the old one should have stayed open --
the original diagnosis was still correct and the fix still shipped.

**Do not trust a plan's checkboxes as a signal of completion.** Nothing ticks
them. Both surviving plans in `docs/superpowers/plans/` show zero of their 66
and 49 boxes checked while their code is demonstrably merged. Verify against
the code -- `grep` for the symbol the entry names -- before believing either a
plan or the backlog.

## Adding a pipeline tool

Registering a tool in `backend/app/pipelines/tools.py` is only half the
change. `backend/app/services/suggestion_service.py` decides which tool each
Actions-tab card recommends, and it is a hand-maintained mapping -- a new tool
that no rule can pick will never be suggested, however cleanly it installs.

The failure mode is silent, which is why this is worth writing down:
installing Flye does not make the Assemble card light up. It leaves a card
reading "No assembler is installed" sitting next to an installed assembler.

So when adding a tool, check `suggestion_service.py` for either a rule that
should now pick it, or a card whose `unavailable` reason has just stopped
being true. The rules have tests in
`backend/tests/services/test_suggestion_service.py`; add the case there.

Third, the Software help page (`/help/software`) renders `TOOL_META`
directly, and `test_every_tool_is_documented` requires every entry to carry
`homepage`, `citation`, `license`, and `usage`. A new tool fails that test
until those are filled in -- which is the point, since the alternative is a
reference page that silently omits it.

Verify the license and citation against the project's own repository rather
than recalling them. A wrong license claim on a page that reads as
authoritative is worse than a blank field, which is why `repository` and
`citation_url` are deliberately *not* required: a tool with no public repo or
no paper should leave them empty rather than invite a fabricated value.

`usage` says how BioFlow uses the tool. Write behaviour, not flags -- flags
change whenever a runner is tuned, and nothing can mechanically catch a
`usage` string that has gone stale. The same applies to
`backend/app/pipelines/sources.py`, which backs `/help/sources` and has its
own completeness test.

A related trap, since it already cost a wrong claim once: the comment on
`ToolMeta.runnable` spent a long time citing cutadapt and Trimmomatic as
tools nothing dispatches to, years after `trim_reads` grew its three-way
dispatch. Nothing failed, because a comment cannot. When a `runnable` value
or the prose around it looks surprising, check
`backend/app/queue/pipeline_handlers.py` for what actually dispatches rather
than trusting the note.

Two traps when testing tool availability, both of which produced tests that
silently read the host machine while appearing to control it:

- `aligner_registry`'s specs are frozen dataclasses that captured
  `tools.minimap2` as a *function object* at import time, so patching
  `app.pipelines.tools.minimap2` never reaches `spec.tool`. Patch `spec_for`
  instead.
- The image ships most tools as installed, so a test asserting a card is
  *available* passes whether or not its patch worked. Assert the card flips
  to unavailable when the probe is patched off -- that is the direction that
  fails when the seam breaks.

## Hand-maintained registries keyed by an enum

`suggestion_service.py`'s rules and `TOOL_META` above are one instance of a
wider shape: a module-level dict keyed by something an enum already
enumerates, where a member the dict has no entry for is simply skipped rather
than raised. Adding STAR found this in `results._SIDECAR_ROLES` and cost a
`build_index` job that reported success while storing none of its eight index
files -- the full test suite stayed green throughout, because every fixture
fed the appliers roles already in the allowlist.

[`docs/superpowers/specs/2026-08-05-registry-audit-design.md`](docs/superpowers/specs/2026-08-05-registry-audit-design.md)
walked every registry named in
[#11](https://github.com/syntheticgio/bioflow/issues/11) and found the shape
splits three ways, which matters because only one of the three should be
"fixed" the same way:

- **Genuinely derivable.** `results._SIDECAR_ROLES` (`{role.value: role for
  role in SidecarRole}`) and `schemas.ROLE_FIELDS` /
  `schemas.FORMAT_FIELDS` -- the last two are dicts holding real per-role or
  per-format data, so a full derivation is wrong, but each has a companion
  `frozenset` (`FORMAT_DERIVED_ROLES`, `FORMAT_COMMON_ONLY`) covering every
  enum member the main dict doesn't, with a comment on each member saying
  why. `set(TheEnum) == set(main_dict) | companion_set` is the exhaustiveness
  test both carry. **This is the pattern to copy.**
- **Intentionally partial, and the wrong instinct is to force coverage.**
  `enrich._TOKEN_SEQUENCE_TYPES` maps an open vocabulary of filename tokens
  to `SequenceType` values; `detect_sequence_type`'s contract is to return
  `None` when a filename doesn't say, and forcing every enum member to be
  reachable by construction would turn "the name doesn't say" into a wrong
  guess -- worse than the STAR failure, not a fix for it. What these need
  instead is a written inclusion rule (why a token belongs) and, where
  checkable, a *reachability* test the other direction: every enum member
  detectable by at least one entry (`test_every_option_is_reachable_by_some_token`
  in `tests/metadata/test_sequence_type.py`).
- **Cannot be derived because the keys belong to something outside this
  repo.** `ncbi_assembly_components.COMPONENTS` is keyed by NCBI's
  `--include` names; a key NCBI doesn't accept fails loudly at the `datasets`
  command line, so there's no silent-skip risk on the key side. The risk is
  internal: `COMPONENT_ORDER`, a hand-written tuple parallel to `COMPONENTS`
  reached by iteration in every function that walks the components, had zero
  tests tying it to the dict
  -- a component added to one and not the other would be invisible in the
  download dialog with no error anywhere, which is the closest structural
  match to the STAR failure of anything the audit found. The fix there was
  `set(COMPONENT_ORDER) == set(COMPONENTS)`, plus uniqueness assertions on
  the fields two different call sites key dicts by (`file_type`,
  `preview_key`).

Before adding a case to a dict shaped like this, check which of the three it
is. Forcing the middle case into the first case's pattern is not more
correct, it's a detector that starts guessing.

**A registry pair -- "classified" and "no double-classification" -- has to be
run together, not read one test at a time.** `node_types.py`'s
`NODE_TYPES`/`EXCLUDED_LAUNCHES` split is the "genuinely derivable" pattern
above, but it's a partition, not just a covering: every launcher must be
classified (`test_every_launch_function_is_classified`, `TestExhaustiveness`
in `backend/tests/pipelines/test_node_types.py`) *and* not classified as both
(`test_no_launcher_is_both_used_and_excluded`). #355 fixed the first test for
`launch_annotation_stats` with two independent commits -- one added a
`NodeTypeSpec`, the other added the exclusion -- and both landed, which
satisfied the test named in the issue's own Verification block while
silently failing the second one in the same class. It stayed red until
someone ran the whole file (fixed in
[#366](https://github.com/syntheticgio/bioflow/pull/366)). When closing an
exhaustiveness gap like this, run the full `TestExhaustiveness` class, not
just the one test the bug report names -- a fix that adds an entry can
collide with a fix that excludes it, and only the partition-completeness test
catches the collision.

## Adding an AI-using feature

AI calls go through `app/services/ai/`, never directly to an HTTP endpoint.
The path is always the same two lines:

```python
provider = await ai.resolve(TaskSlot.YOUR_SLOT)   # None means nothing configured
result = await ai.complete(provider, system=..., user=...)
```

Three things about that are easy to get wrong.

**A new feature needs a new `TaskSlot` member** in `app/models/ai.py`, plus a
label in `_SLOT_LABELS`. The settings page renders one row per member, so the
enum is what makes a feature routable -- a call site that reuses
`FILE_SUMMARY` because it is already there silently ties two unrelated
features to one provider, and the user has no way to separate them.

**`complete()` never raises and never returns None.** It returns `Completion`
or `Failure`. Checking `if result is None` -- the shape the old `llm_client`
had -- passes type-checking, reads as correct, and treats every failure as a
success. Check `isinstance(result, Completion)`.

**Thread handlers must not call `asyncio.run()` to reach an async AI helper.**
`HandlerMode.THREAD` handlers run in a worker-pool thread with no event loop,
and `asyncio.run()` looks like the obvious way to get one -- but this
process's Mongo client is a module-level `AsyncIOMotorClient` bound to the
loop `connect_to_mongo()` ran on, and a second, unrelated loop makes Motor
raise "attached to a different loop" the instant a query touches it. This
is not hypothetical: it is exactly what broke `summarize_object` the first
time it ran against a real configured provider, with every unit test green,
because every test mocks the seam that made the call and never exercises the
real event-loop plumbing underneath it. Use `app.db.client.run_from_thread`
instead -- it schedules the coroutine onto the stored connect-time loop via
`asyncio.run_coroutine_threadsafe` and blocks the calling thread for the
result, the same pattern `queue/executor.py`'s `_schedule_lease_extension`
already uses for the identical problem. `summary_handlers.py`'s
`_resolve_sync()` is the worked example. Thread handlers also get no
automatic failure recording from `complete()` (that write needs the loop
too) -- use `complete_sync()`, which skips it, and return the failure reason
in the handler's own result payload instead.

Failures are recorded on the provider document, which is what the settings
badge reads. That means a provider can go red from a real job rather than only
from pressing "Fetch models" -- deliberate, and the reason an expired key is
visible rather than silently stopping summaries.

## Querying computation records

`job_timings` (`app/models/timing.py`) holds one row per completed job, read
by three different consumers: the duration model, the memory model, and
per-object provenance. **Failed runs are in there too**, and the difference
between the consumers is whether they want them.

This is a deliberate trade recorded in
`docs/superpowers/specs/2026-08-03-computation-records-design.md`: provenance
and OOM detection need failures, the predictive models must never see them.
Before that design, the collection held successes only -- `executor.py`
recorded on the success path and nowhere else -- so no query needed an outcome
filter and none had one.

That invariant now lives in the read paths instead of the write path, which is
the part that is easy to get wrong. **Do not query the collection directly to
fit or summarize anything.** Go through `timing_service._modelled()` (the
outcome-filtered accessor both `_samples()`, the duration model, and
`estimate_memory()`/`stats()`, the memory model, are built on) or one of its
callers; provenance uses `records_for_object()`, its own explicitly-named
accessor that includes failures on purpose.

The failure mode if you forget is silent and points the wrong way. A job that
OOM-killed at ninety seconds looks like a fast, cheap run, and its peak RSS is
the ceiling it *hit* rather than what it needed. A few of those in a fit drag
estimates downward -- predicting that jobs are cheaper than they are, which is
exactly the direction that causes the next OOM. Nothing raises, no test fails
on its own; the numbers are just quietly low.

Resource fields (`peak_rss_bytes`, `peak_cpu_percent`) are null for runs under
60 seconds, so anything fitting against them is working with a subset of rows
rather than all of them. A row with a null peak is a short job, not a job whose
sampling failed.
