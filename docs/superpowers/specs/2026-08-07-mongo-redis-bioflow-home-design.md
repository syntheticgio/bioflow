# Move Mongo/Redis into ~/.bioflow

Issue: [#75](https://github.com/syntheticgio/bioflow/issues/75)
Parent epic: [#29 — Migrate volume](https://github.com/syntheticgio/bioflow/issues/29)

## Problem

`mongo-data` and `redis-data` are named Docker volumes today, so Docker
places their contents under its own managed storage (on macOS, inside the
Docker Desktop VM disk image), not under any path BioFlow's own docs or
launcher know about.

This is the blocker for the sibling issue, [#76 — migrate `BIOINFO_HOME`
storage location](https://github.com/syntheticgio/bioflow/issues/76): a
"move BioFlow's storage" operation should not have to also move a live
database. Getting Mongo/Redis into the fixed `~/.bioflow` install directory
first means #76 only ever has to copy inert data files under `BIOINFO_HOME`.

This is a single-user, single-machine, one-off change per
[CLAUDE.md](../../../CLAUDE.md): no general migration tooling, no
configurability, no concern for other users' machines. `~/.bioflow` is a
fixed path, matching how the launcher's Rust code already treats
`install_dir` (see `fixed_install_dir()` in
`launcher/src-tauri/src/commands.rs`) as non-configurable, unlike
`BIOINFO_HOME`/storage location which the user can change.

## Non-goals

- No support for relocating `~/.bioflow` itself — it stays fixed, per user
  decision in brainstorming.
- No general/repeatable migration mechanism — this is a one-time manual data
  copy on this one machine, done as part of landing the code change, not a
  feature.
- No change to `BIOINFO_HOME`/the storage-location migration — that is
  issue #76, which depends on this one but is out of scope here.
- No change to worktree, review, or test stack behavior or isolation — see
  below, this must be a no-op for them.

## Design

### `docker-compose.yml`

Change the `mongo` and `redis` services from named volumes to bind mounts
under the fixed `~/.bioflow` path, expanded via the host's `${HOME}` (Compose
expands host environment variables, not container ones, at parse time):

```yaml
services:
  mongo:
    volumes:
      - ${HOME}/.bioflow/mongo:/data/db
      - ./ops/mongo-init:/docker-entrypoint-initdb.d:ro

  redis:
    volumes:
      - ${HOME}/.bioflow/redis:/data
      - ./ops/redis/redis.conf:/usr/local/etc/redis/redis.conf:ro

# mongo-data and redis-data removed from the top-level volumes: block --
# nothing declares them anymore.
```

No new environment variable, no `.env` change, no launcher (Rust) change:
the launcher bundles/copies `docker-compose.yml` verbatim (see
`launcher/README.md` and the comment on `BUNDLED_COMPOSE_RESOURCE` in
`launcher/src-tauri/src/commands.rs`), so it inherits this bind mount for
free the same way it inherits every other service definition.

### Why this must not affect worktree/review/test stacks

`docker-compose.yml` is not exclusive to the one real installation — every
worktree, review, and throwaway test stack on this machine reads the same
file, distinguished only by `COMPOSE_PROJECT_NAME` (see
`ops/worktree-up.sh`). Today that gives each stack its own Mongo/Redis for
free, because Compose namespaces *named* volumes by project
(`biopipe_mongo-data` vs `biopipe-wt-<slug>_mongo-data`).

A bind mount to a literal fixed path has no such namespacing. Left
unaddressed, every worktree stack would point at the exact same
`~/.bioflow/mongo` directory as the main stack and as each other --
`ops/worktree-up.sh`'s `mongodump`/`mongorestore` seeding and its
`compose down -v` cleanup both assume isolated, per-project storage, and
this would break both, with real corruption risk if two `mongod` processes
ever pointed at the same WiredTiger files concurrently.

### `ops/docker-compose.worktree.yml`

This file already exists solely to keep worktree stacks from colliding with
the main one (it overrides `ports:` with `!override`, per its own header
comment). Add a matching volume override so worktree stacks keep isolated,
project-namespaced named volumes exactly as they do today:

```yaml
services:
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

`!override` matters here for the same reason it already matters for
`ports:` in this file: Compose *merges* (appends/extends) volume lists
across files by default rather than replacing them, so without the tag a
worktree stack's mongo container could end up with both the bind mount and
the named volume mounted at overlapping paths.

Since `ops/worktree-up.sh` already applies this file on every worktree
invocation (`-f "$WT_ROOT/ops/docker-compose.worktree.yml"`), no change to
that script is needed. Every worktree, review, and test stack -- including
the several with agents currently in progress in other worktrees at the
time of this design -- is unaffected: each is working from its own
checked-out snapshot of these compose files until it merges, and by the
time it merges this override will already be in place.

### One-time data migration on this machine

Manual, done once, not part of any script or launcher code:

1. Stop the main `biopipe` stack (`docker compose down` from the main
   checkout).
2. Create `~/.bioflow/mongo` and `~/.bioflow/redis`.
3. Raw file copy from each existing named volume into the new bind-mount
   path, e.g.:
   ```bash
   docker run --rm \
     -v biopipe_mongo-data:/from -v ~/.bioflow/mongo:/to \
     alpine cp -a /from/. /to/

   docker run --rm \
     -v biopipe_redis-data:/from -v ~/.bioflow/redis:/to \
     alpine cp -a /from/. /to/
   ```
   Raw copy rather than `mongodump`/`mongorestore`: the stack is fully
   stopped for the whole operation, so there is no live writer and no
   WiredTiger consistency concern that only a logical dump would avoid.
4. Bring the stack back up (`docker compose up -d --build api web worker`
   per this repo's standing instructions) and verify against the real data
   (existing projects visible, queue/job state intact).
5. Once verified, remove the old named volumes
   (`docker volume rm biopipe_mongo-data biopipe_redis-data`).

## Testing

- Bring up the main stack after the compose change and the manual data
  copy; confirm existing projects, jobs, and queue state are all present
  (this is the real check called out in CLAUDE.md's "check a rule against
  the real database" guidance -- a config change like this is exactly the
  kind of thing that can look right in isolation and still be wrong against
  real data).
- Confirm `~/.bioflow/mongo` and `~/.bioflow/redis` are populated and the
  stack writes to them (e.g. `ls` timestamps change after running a job).
- Run `./ops/worktree-up.sh` from an existing worktree and confirm it still
  creates its own isolated, project-namespaced Mongo/Redis exactly as
  before -- this is the regression this design is built to avoid, so it
  needs an explicit check, not an assumption.
- `docker inspect biopipe-mongo-1 --format '{{range .Mounts}}...'` to
  confirm the running main stack's mongo container is bind-mounted from
  `~/.bioflow/mongo`, not a named volume.
