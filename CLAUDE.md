# Working on this repo

Single-user, local-only tool for non-critical, non-essential work. There is no
production deployment and no other users to protect. Optimize for "the person
using it can see their change" over correctness-at-scale practices that make
sense for a team or a hosted service.

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

**Always run `docker compose` from the main repo root, never from a
worktree.** The bind mounts in `docker-compose.override.yml` are relative
paths (`./backend/app`, `./frontend/src`), so Compose resolves them against
whatever directory it was invoked from -- while the project name is pinned to
`biopipe` in `docker-compose.yml`. Running `docker compose up` inside
`.claude/worktrees/<something>/` therefore does not create a second stack: it
silently recreates *the* stack with its source pointing at that worktree, with
no error and no warning. Port 5173 then serves that branch's code, and
`docker compose restart worker` from anywhere just reloads it again -- so a
change merged to main appears to be missing from the running app, and the
handler simply never shows up in the worker's `handlers_loaded` log line.

To check what the stack is actually serving:

```bash
docker inspect biopipe-worker-1 --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}'
```

If any source path contains `.claude/worktrees/`, the stack is on the wrong
tree; fix it by re-running the rebuild from the main repo root:

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner && docker compose up -d --build api web worker
```

To exercise a worktree's code before it merges, don't repoint the shared
stack -- use a separate project name and unpublished ports (`docker compose
-p biopipe-<branch> ...`) so the main instance keeps serving main.

## Verifying changes

Manual testing in the browser at localhost:5173 is the actual verification
step for anything UI-facing -- there is no headless component-testing setup
in this repo (no jsdom/testing-library, zero `.test.tsx` files) and none is
expected. Backend changes are covered by `pytest`; run it inside the `api`
container (`docker compose exec api python -m pytest tests/ -q`) rather than
a bare host `.venv`, since the host venv hits Mongo replica-set connection
errors that the container's network doesn't have.

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

Delete an entry outright only when it was wrong to begin with -- not merely
done. A `— FIXED` entry is a record; a deleted one is a question someone will
ask again.

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
