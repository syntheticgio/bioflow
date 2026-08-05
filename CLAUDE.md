# Working on this repo

Single-user, local-only tool for non-critical, non-essential work. Optimize for
"the person using it can see their change" over correctness-at-scale practices
that make sense for a team or a hosted service.

**`main` is this project's dev branch, not its production branch.** A separate
downstream process does the final testing before anything reaches prod, so
landing on `main` is not shipping to users -- it is the equivalent of pushing
to a shared dev trunk.

## Commit and merge once the tests are green, without asking

The consequence of the above: **do not hesitate over committing, merging to
`main`, or pushing.** Once the suite is green, commit. If the work is done and
`main` is clean, merge and push. Stopping to ask permission for each of those
is friction with nothing on the other side of it -- there is no review gate to
respect and no user-facing release to gate, and the downstream process is what
actually protects prod.

What still earns a pause:

- **A red or unrun suite.** "Green" is the precondition, and it means read the
  count, not the exit code of whatever was last in the pipeline.
- **`main` not being clean.** Merge into a dirty or diverged `main` and the
  conflict becomes someone else's. Multiple agents merging to `main`
  concurrently is a known rough edge being worked on separately; if `main` has
  moved under you, re-run the suite after merging rather than assuming your
  green still holds.
- **Anything genuinely destructive** -- history rewrites, force pushes,
  deleting branches that hold unmerged work. Committing is cheap and
  reversible; those are not.

Keep commits separable: a mechanical rename and a behaviour change in one
commit is a commit nobody can review or revert, whatever the test count says.

## Push to origin when a merge to main is the end of the task

`origin` (`github.com:syntheticgio/bioflow`) is the remote this project
actually uses. When a task's work lands on `main` and the task is otherwise
done -- not a mid-task checkpoint, not a merge with more steps still to come --
push `main` to `origin` as part of finishing, rather than leaving it local
only. A merge that stays unpushed is a merge someone still has to remember to
push later, and there is no other workflow here (no PR review gate, no CI)
that does it instead.

## Update issue when a task is completed or there is significant progress
You should update the issue that we are tracking a task with in Github with any
significant progress.  Specifically when the spec is done or the implementation is done
or the entire task is done.  The appropriate tags should be used - status:specification document
means that the spec needs to be written, status:implementation plan means
that the implementation plan needs to be written and status: ready means it
is ready to implement.  The other labels are self explanatory and should be
used when appropriate.

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

**"Merged to `main` and tested to the best of your ability" is the bar for
`— FIXED` -- don't hold an entry open waiting for the user's own later
testing to bless it.** `main` is a dev trunk here, not a release
([above](#working-on-this-repo)); nothing in this repo ships to users at
merge time, so there is no "final testing" step downstream of you that a
TODO entry should wait on. If testing after merge turns up a real problem,
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
