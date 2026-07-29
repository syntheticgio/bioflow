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
