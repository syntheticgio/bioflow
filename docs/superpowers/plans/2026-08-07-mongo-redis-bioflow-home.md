# Move Mongo/Redis into ~/.bioflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move Mongo and Redis data from Docker-managed named volumes into bind mounts under the fixed `~/.bioflow` directory, for the single real installation on this machine, without disturbing worktree/review/test stack isolation.

**Architecture:** Two compose-file edits (base file switches to bind mounts, worktree overlay restores isolated named volumes) plus a one-time manual raw-file-copy migration of this machine's existing `biopipe_mongo-data`/`biopipe_redis-data` volumes into `~/.bioflow/mongo` and `~/.bioflow/redis`.

**Tech Stack:** Docker Compose (YAML), bash, Docker CLI.

---

Spec: [docs/superpowers/specs/2026-08-07-mongo-redis-bioflow-home-design.md](../specs/2026-08-07-mongo-redis-bioflow-home-design.md)
Issue: [#75](https://github.com/syntheticgio/bioflow/issues/75)

## Context for the engineer

`docker-compose.yml` is shared by every stack on this machine — the one real
`biopipe` install *and* every disposable worktree/review/test stack, which
get their own project name via `COMPOSE_PROJECT_NAME` (see
`ops/worktree-up.sh`). Compose currently gives each of those stacks its own
isolated Mongo/Redis for free, because named volumes are namespaced by
project (`biopipe_mongo-data` vs `biopipe-wt-<slug>_mongo-data`).

This plan changes the *base* file to a bind mount at a fixed host path
(`~/.bioflow/mongo`, `~/.bioflow/redis`), which has no such namespacing. To
avoid breaking worktree isolation, `ops/docker-compose.worktree.yml` — which
worktree stacks already load on top of the base file — must override those
two services back to isolated named volumes, the same way it already
overrides `ports:`.

Current relevant lines in `docker-compose.yml` (repo root):

```yaml
  mongo:
    image: mongo:7
    command: ["--replSet", "rs0", "--bind_ip_all"]
    volumes:
      - mongo-data:/data/db
      - ./ops/mongo-init:/docker-entrypoint-initdb.d:ro
    healthcheck:
      test: ["CMD", "mongosh", "--quiet", "--eval", "try { rs.status().ok } catch (e) { rs.initiate({_id:'rs0',members:[{_id:0,host:'mongo:27017'}]}).ok }"]
      interval: 10s
      timeout: 5s
      retries: 12
      start_period: 20s
    restart: unless-stopped

  redis:
    image: redis:7-alpine
    command: ["redis-server", "/usr/local/etc/redis/redis.conf"]
    volumes:
      - redis-data:/data
      - ./ops/redis/redis.conf:/usr/local/etc/redis/redis.conf:ro
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 10
    restart: unless-stopped
```

and at the bottom:

```yaml
volumes:
  mongo-data:
  redis-data:
```

Current full contents of `ops/docker-compose.worktree.yml`:

```yaml
# Port overrides for a worktree stack. Applied by ops/worktree-up.sh on top of
# docker-compose.yml and docker-compose.override.yml, never on its own.
#
# `!override` matters here: Compose *appends* to a `ports` list when merging
# files rather than replacing it, so without the tag a worktree stack would
# publish 5173 and 8000 in addition to its own ports -- colliding with the
# main stack, which is the one thing this file exists to avoid.
services:
  api:
    ports: !override
      - "${WT_API_PORT:-8100}:8000"

  web:
    ports: !override
      - "${WT_WEB_PORT:-5273}:80"
```

This machine currently has these named volumes for the main stack (confirmed
via `docker volume ls` / `docker volume inspect` during brainstorming):
`biopipe_mongo-data` (created 2026-07-25) and `biopipe_redis-data` (created
2026-07-25). The main stack is normally run via plain `docker compose up`
from the repo root (not the launcher) per this repo's own CLAUDE.md.

---

### Task 1: Switch `docker-compose.yml` to bind mounts under `~/.bioflow`

**Files:**
- Modify: `docker-compose.yml`

- [ ] **Step 1: Change the `mongo` service's `volumes:` to a bind mount**

In `docker-compose.yml`, find the `mongo` service and replace its `volumes:`
block:

```yaml
    volumes:
      - mongo-data:/data/db
      - ./ops/mongo-init:/docker-entrypoint-initdb.d:ro
```

with:

```yaml
    volumes:
      # Bind-mounted to the fixed ~/.bioflow install directory rather than a
      # named volume, so the database lives at a known host path instead of
      # inside Docker's own managed storage. ~/.bioflow is intentionally
      # fixed and not user-configurable (see launcher/src-tauri/src/commands.rs
      # fixed_install_dir()) -- unlike BIOINFO_HOME/storage location, which
      # the user can change. See docs/superpowers/specs/2026-08-07-mongo-redis-bioflow-home-design.md.
      - ${HOME}/.bioflow/mongo:/data/db
      - ./ops/mongo-init:/docker-entrypoint-initdb.d:ro
```

- [ ] **Step 2: Change the `redis` service's `volumes:` to a bind mount**

Replace:

```yaml
    volumes:
      - redis-data:/data
      - ./ops/redis/redis.conf:/usr/local/etc/redis/redis.conf:ro
```

with:

```yaml
    volumes:
      # See the comment on the mongo service's volumes above.
      - ${HOME}/.bioflow/redis:/data
      - ./ops/redis/redis.conf:/usr/local/etc/redis/redis.conf:ro
```

- [ ] **Step 3: Remove the now-unused named volume declarations**

At the bottom of `docker-compose.yml`, remove the `mongo-data:` and
`redis-data:` lines from the `volumes:` block. If nothing else declares a
volume there, delete the `volumes:` block entirely.

Before:

```yaml
volumes:
  mongo-data:
  redis-data:
```

After: this block is removed entirely (nothing else in this file declares a
top-level named volume as of this plan).

- [ ] **Step 4: Validate the compose file parses**

Run:

```bash
docker compose -f docker-compose.yml config -q
```

Expected: no output, exit code 0. This validates YAML syntax and Compose
schema without starting anything.

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml
git commit -m "infra: bind-mount mongo/redis under ~/.bioflow instead of named volumes"
```

---

### Task 2: Restore isolated named volumes for worktree stacks

**Files:**
- Modify: `ops/docker-compose.worktree.yml`

- [ ] **Step 1: Add volume overrides for `mongo` and `redis`**

Replace the full contents of `ops/docker-compose.worktree.yml` with:

```yaml
# Port overrides for a worktree stack. Applied by ops/worktree-up.sh on top of
# docker-compose.yml and docker-compose.override.yml, never on its own.
#
# `!override` matters here: Compose *appends* to a `ports` list when merging
# files rather than replacing it, so without the tag a worktree stack would
# publish 5173 and 8000 in addition to its own ports -- colliding with the
# main stack, which is the one thing this file exists to avoid.
services:
  api:
    ports: !override
      - "${WT_API_PORT:-8100}:8000"

  web:
    ports: !override
      - "${WT_WEB_PORT:-5273}:80"

  # docker-compose.yml bind-mounts mongo/redis to a single fixed host path
  # (~/.bioflow) for the one real installation. That path has no per-project
  # namespacing, so left alone every worktree stack would point at the exact
  # same directory as the main stack and as each other -- breaking
  # ops/worktree-up.sh's mongodump/mongorestore seeding and its
  # `compose down -v` cleanup, with real corruption risk if two mongod
  # processes ever pointed at the same WiredTiger files concurrently. These
  # overrides restore the isolated, project-namespaced named volumes that
  # worktree stacks relied on before that change. See
  # docs/superpowers/specs/2026-08-07-mongo-redis-bioflow-home-design.md.
  mongo:
    volumes: !override
      - mongo-data:/data/db
      - ./ops/mongo-init:/docker-entrypoint-initdb.d:ro

  redis:
    volumes: !override
      - redis-data:/data
      - ./ops/redis/redis.conf:/usr/local/etc/redis/redis.conf:ro

volumes:
  mongo-data:
  redis-data:
```

- [ ] **Step 2: Validate the merged worktree compose config parses**

Run (from the repo root; this mirrors how `ops/worktree-up.sh` invokes
compose):

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.override.yml \
  -f ops/docker-compose.worktree.yml \
  config -q
```

Expected: no output, exit code 0.

- [ ] **Step 3: Confirm the merged config resolves mongo/redis to named volumes, not the bind mount**

Run:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.override.yml \
  -f ops/docker-compose.worktree.yml \
  config | grep -A3 -E '^\s*(mongo|redis):' | grep -A3 volumes
```

Expected: the `mongo` and `redis` sections show `mongo-data`/`redis-data` as
the volume source (e.g. a line containing `source: mongo-data`), not a path
under `.bioflow`.

- [ ] **Step 4: Commit**

```bash
git add ops/docker-compose.worktree.yml
git commit -m "infra: keep worktree stacks on isolated named volumes for mongo/redis"
```

---

### Task 3: Verify worktree isolation is unaffected (from an existing worktree)

**Files:** none (verification only)

- [ ] **Step 1: Run `worktree-up.sh` from an existing worktree**

This plan's own worktree checkout is a valid target. From this worktree's
root:

```bash
./ops/worktree-up.sh
```

Expected: it builds and starts a stack under a `biopipe-wt-<slug>` project
name, on ports 5273/8100, and either seeds or reports an existing database —
per the script's own output. It must **not** print any Docker error about a
mount conflict, and must not affect the main stack on 5173.

- [ ] **Step 2: Confirm the worktree stack's mongo container uses a named volume, not the bind mount**

```bash
docker inspect $(docker ps --filter "name=mongo-1" --filter "name=biopipe-wt-" --format '{{.Names}}' | head -1) \
  --format '{{range .Mounts}}{{.Type}} {{.Name}}{{.Source}}{{"\n"}}{{end}}'
```

Expected: a line with `Type` `volume` and a `Name` like
`biopipe-wt-<slug>_mongo-data` — not `Type` `bind` with a `Source` under
`.bioflow`.

- [ ] **Step 3: Tear the worktree stack down**

```bash
./ops/worktree-up.sh --down
```

Expected: stack stops and its volumes are deleted, per the script's existing
`compose down -v` behavior. This does not touch `~/.bioflow` since the
worktree stack was never bind-mounted there.

---

### Task 4: One-time data migration for this machine's main stack

**Files:** none (manual data operation, not a code change)

This task moves this machine's existing `biopipe_mongo-data` and
`biopipe_redis-data` named volumes into `~/.bioflow/mongo` and
`~/.bioflow/redis`, so the main stack's real data survives the Task 1
compose change. This is a one-off operation on this machine, not a script to
commit — per the spec, this repo does not want general migration tooling for
a single-user, single-machine setup.

- [ ] **Step 1: Confirm the volumes that will be migrated**

```bash
docker volume ls | grep -E '^local\s+biopipe_(mongo|redis)-data$'
```

Expected: both `biopipe_mongo-data` and `biopipe_redis-data` listed. If
either is missing, stop and investigate before proceeding — do not create
fresh empty directories and silently lose data.

- [ ] **Step 2: Stop the main stack**

From the main checkout root (not this worktree):

```bash
docker compose down
```

Expected: `biopipe-mongo-1`, `biopipe-redis-1`, `biopipe-api-1`,
`biopipe-worker-1`, `biopipe-web-1` all stop and are removed. Named volumes
are preserved (`down` without `-v`).

- [ ] **Step 3: Create the destination directories**

```bash
mkdir -p ~/.bioflow/mongo ~/.bioflow/redis
```

- [ ] **Step 4: Raw-copy the mongo volume contents**

```bash
docker run --rm \
  -v biopipe_mongo-data:/from \
  -v ~/.bioflow/mongo:/to \
  alpine cp -a /from/. /to/
```

Expected: exits 0 with no output. Raw copy is safe here because the stack is
fully stopped — there is no live Mongo process holding the WiredTiger files
open, so no dump/restore is needed (per the spec's explicit decision).

- [ ] **Step 5: Raw-copy the redis volume contents**

```bash
docker run --rm \
  -v biopipe_redis-data:/from \
  -v ~/.bioflow/redis:/to \
  alpine cp -a /from/. /to/
```

Expected: exits 0 with no output.

- [ ] **Step 6: Verify the copies landed**

```bash
ls -la ~/.bioflow/mongo
ls -la ~/.bioflow/redis
```

Expected: `~/.bioflow/mongo` contains Mongo's data files (e.g. `WiredTiger`,
`*.wt`, a `journal/` directory). `~/.bioflow/redis` contains Redis's
persisted files (e.g. `appendonlydir/`, matching the `appendonly yes` config
in `ops/redis/redis.conf`).

- [ ] **Step 7: Bring the stack up from the now-updated `docker-compose.yml`**

From the main checkout root:

```bash
docker compose up -d --build api web worker
```

(This is the standing rebuild command from this repo's CLAUDE.md — it
applies the Task 1 compose change and starts everything, including mongo and
redis via their new bind mounts, since `api`/`worker`/`web` all depend on
them.)

- [ ] **Step 8: Confirm mongo is now bind-mounted from `~/.bioflow/mongo`**

```bash
docker inspect biopipe-mongo-1 --format '{{range .Mounts}}{{.Type}} {{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'
```

Expected: a line reading `bind /Users/<you>/.bioflow/mongo -> /data/db` (not
`volume`).

- [ ] **Step 9: Confirm redis is now bind-mounted from `~/.bioflow/redis`**

```bash
docker inspect biopipe-redis-1 --format '{{range .Mounts}}{{.Type}} {{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'
```

Expected: a line reading `bind /Users/<you>/.bioflow/redis -> /data` (not
`volume`).

- [ ] **Step 10: Verify real data survived the move**

Open `http://localhost:5173` in a browser (or `curl
http://localhost:8000/healthz`) and confirm existing projects and data are
still visible — this is the check CLAUDE.md calls out directly: verify
against the real database, not just that the containers started. If
projects, jobs, or files that existed before Task 4 Step 2 are missing,
stop here; do not proceed to Step 11 until the discrepancy is understood
(the old named volumes are still intact and untouched at this point, so
nothing has been lost yet).

- [ ] **Step 11: Remove the old named volumes**

Only after Step 10 has confirmed the real data is present and correct:

```bash
docker volume rm biopipe_mongo-data biopipe_redis-data
```

Expected: both removed with no error (this fails if any container still
references them, which would mean Step 7 didn't actually pick up the new
compose file — re-check Step 8/9 first if this errors).

---

## Self-Review Notes

- **Spec coverage:** `docker-compose.yml` bind mount (Task 1), matches spec.
  `ops/docker-compose.worktree.yml` override (Task 2), matches spec. Worktree
  isolation verification (Task 3), matches spec's testing section. One-time
  manual migration with raw copy (Task 4), matches spec's migration section
  and the user's explicit choice of raw copy over dump/restore. No launcher
  code changes, matching the spec's explicit statement that the launcher
  inherits this for free.
- **Placeholder scan:** no TBD/TODO; all commands are concrete and copy-paste
  ready; expected output is stated for every verification step.
- **Type consistency:** N/A — no application code, only YAML and shell.
