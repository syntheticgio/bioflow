# Working on this repo

Single-user, local-only tool for non-critical, non-essential work. Optimize for
"the person using it can see their change" over correctness-at-scale practices
that make sense for a team or a hosted service.

**`main` is this project's dev trunk, not its production branch.** Shipping to
users happens at a tag, from a release branch, several stages downstream of
`main` (see [Release methodology](#release-methodology) below). Landing on
`main` is not shipping.

**But `main` is now reached through a pull request, not a direct merge.** This
changed on 2026-08-09. The rest of the pipeline -- alpha, beta, production --
is described under [Release methodology](#release-methodology); this section
is only about how a piece of work gets from a worktree onto `main`.

## Finish work on a branch, push it, and open a PR

**You may merge your own PR to `main` once all required CI checks pass.** This
changed on 2026-08-17: the user authorized agents to merge routine work without
waiting for a review, gated on a green suite. If a change is unusually large or
design-sensitive, or the user asked to review first, still leave it as an open
PR and report the URL. Committing, pushing, and merging are all yours to do.

**Before opening the PR, catch up to `main` yourself.** `main` moves while a
task is in progress; rebasing before you push is what makes the PR mergeable
the moment it exists, instead of leaving GitHub to discover a conflict later:

```bash
git fetch origin main
git rebase origin/main
```

If the rebase conflicts badly enough that per-commit resolution isn't
practical, fall back to a merge instead:

```bash
git rebase --abort
git merge origin/main
```

Then confirm the work survived the rebase/merge before pushing -- a conflict
resolution can silently drop a hunk:

```bash
git diff origin/main...HEAD --stat
```

Once that's checked and the suite is green:

```bash
git push -u origin HEAD
```

```bash
gh pr create --base main --fill
```

Then report the PR URL and, once all required checks are green, merge with
`gh pr merge --squash --delete-branch`. Do not force-push to `main` directly.

What still earns a pause before you push:

- **A red or unrun suite.** "Green" is the precondition, and it means read the
  count, not the exit code of whatever was last in the pipeline.
- **Anything genuinely destructive** -- history rewrites, force pushes,
  deleting branches that hold unmerged work. Committing is cheap and
  reversible; those are not.

If GitHub still reports a conflict after all this (`main` moved again while
CI ran), rebase your branch on `origin/main` and push again -- that is
ordinary work on your own branch, not a history rewrite of a shared one.

Keep commits separable: a mechanical rename and a behaviour change in one
commit is a commit nobody can review or revert, whatever the test count says.
This mattered less when nobody read the commits; now someone does.

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
number when there is one. Agent-generated branch names still work and nothing
rejects them, but they say nothing about the change, and they now show up in a
PR list that someone reads.

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

## Filing out-of-scope issues you find along the way

If you notice a problem that is outside the scope of the current ticket --
unrelated dead code, a stale doc, a missing test, a bug in a different area --
and it does not block finishing the current implementation, file it as a new
GitHub issue yourself. Don't ask first; this is pre-authorized.

Give the issue a clear title and description (what you saw, where, why it
matters), and label it (`type:`, `area:`, `status:`, `priority:`,
`difficulty:`) so it slots into the existing triage flow. Then keep working
on the original task -- filing the issue is a side note, not a detour.

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
`.Codex/settings.json`) blocks bare `docker compose` from a worktree for
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
`.Codex/worktrees/`.

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
the *merge*, not the PR: since you no longer merge your own work, an entry
whose PR is open but unmerged is not yet `— FIXED`. If the PR is open and the
task is otherwise done, say so and leave the entry open. If testing after
merge turns up a real problem,
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
