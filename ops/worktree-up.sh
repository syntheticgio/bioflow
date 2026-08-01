#!/usr/bin/env bash
# Bring up a second stack serving *this worktree's* code, leaving the one on
# port 5173 alone.
#
# Why this exists: the bind mounts in docker-compose.override.yml are relative
# paths and docker-compose.yml pins `name: biopipe`, so running plain
# `docker compose up` from a worktree does not create a second stack -- it
# silently recreates *the* stack with its source pointing here. Port 5173 then
# serves this branch's code, with no error and no warning, and a change merged
# to main appears to have vanished from the running app.
#
# The lever is COMPOSE_PROJECT_NAME, which outranks the pinned `name:`. A
# distinct project gets its own containers, network, and mongo/redis volumes
# for free; ops/docker-compose.worktree.yml republishes the ports so nothing
# collides with the main stack.
#
# BIOINFO_HOME is *shared* with the main stack, deliberately: the point of a
# worktree stack is to exercise a change against the real files, and CLAUDE.md
# already argues that checking a rule against a real project beats another
# fixture. The cost is that a job run here writes into the same /data the main
# stack reads. Fine for a UI or read-path check; think twice before running a
# pipeline that rewrites an existing artifact.
#
# Mongo is *not* shared -- a fresh project gets an empty database, which would
# defeat the purpose -- so first launch copies the main stack's `biopipe`
# database in. That copy is a point-in-time snapshot, not a live mirror.
#
# Usage:  ./ops/worktree-up.sh             # build, start, seed on first run
#         ./ops/worktree-up.sh --reseed    # re-copy the main stack's database
#         ./ops/worktree-up.sh --down      # stop it and delete its volumes
#
# Ports default to 5273 (web) and 8100 (api); override with WT_WEB_PORT and
# WT_API_PORT if you want two worktree stacks at once.
set -euo pipefail

MAIN_PROJECT="biopipe"
MAIN_MONGO="${MAIN_PROJECT}-mongo-1"

WT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# The main checkout, whatever it is called on this machine. `--git-common-dir`
# is the shared .git of the whole repo, so its parent is the main working tree
# even when this script runs from a worktree (whose own .git is a file).
GIT_COMMON="$(git -C "$WT_ROOT" rev-parse --path-format=absolute --git-common-dir)"
MAIN_ROOT="$(dirname "$GIT_COMMON")"

if [ "$WT_ROOT" = "$MAIN_ROOT" ]; then
  echo "This is the main checkout, not a worktree. Use the normal command:" >&2
  echo "  docker compose up -d --build api web worker" >&2
  exit 1
fi

BRANCH="$(git -C "$WT_ROOT" branch --show-current || true)"
[ -n "$BRANCH" ] || BRANCH="$(basename "$WT_ROOT")"
# Compose project names allow lowercase alphanumerics, hyphens, underscores.
SLUG="$(printf '%s' "$BRANCH" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9_-' '-' | sed 's/^-*//; s/-*$//')"

PROJECT="${MAIN_PROJECT}-wt-${SLUG}"
if [ "$PROJECT" = "$MAIN_PROJECT" ]; then
  echo "Refusing to act on the main project name." >&2
  exit 1
fi

# The main checkout's .env, since .env is gitignored and a worktree has none.
# Without this BIOINFO_HOME falls back to the compose-file default, and Docker
# silently creates that path as an empty directory rather than failing.
ENV_FILE="$MAIN_ROOT/.env"
if [ ! -f "$ENV_FILE" ]; then
  echo "No .env at $ENV_FILE. Run: cp $MAIN_ROOT/.env.example $ENV_FILE" >&2
  exit 1
fi

export COMPOSE_PROJECT_NAME="$PROJECT"
export WT_WEB_PORT="${WT_WEB_PORT:-5273}"
export WT_API_PORT="${WT_API_PORT:-8100}"

compose() {
  docker compose \
    --project-directory "$WT_ROOT" \
    --env-file "$ENV_FILE" \
    -f "$WT_ROOT/docker-compose.yml" \
    -f "$WT_ROOT/docker-compose.override.yml" \
    -f "$WT_ROOT/ops/docker-compose.worktree.yml" \
    "$@"
}

MODE="up"
case "${1:-}" in
  --down)   MODE="down" ;;
  --reseed) MODE="reseed" ;;
  "")       ;;
  *)        echo "Unknown option: $1 (expected --down or --reseed)" >&2; exit 1 ;;
esac

if [ "$MODE" = "down" ]; then
  echo "Stopping $PROJECT and deleting its volumes..."
  compose down -v
  exit 0
fi

# Copy the main stack's database into this stack's mongo.
#
# Guarded twice on purpose: this is the one operation here that could damage
# the real database if the direction were ever inverted.
seed_mongo() {
  local target="${PROJECT}-mongo-1"

  if [ "$target" = "$MAIN_MONGO" ]; then
    echo "Refusing to restore into the main stack's mongo." >&2
    exit 1
  fi
  if ! docker inspect "$MAIN_MONGO" >/dev/null 2>&1; then
    echo "The main stack's mongo ($MAIN_MONGO) is not running; nothing to copy." >&2
    echo "Start it from $MAIN_ROOT, or continue with an empty database." >&2
    return 0
  fi

  echo "Copying the biopipe database from $MAIN_MONGO into $target..."
  docker exec "$MAIN_MONGO" mongodump --db biopipe --archive --quiet \
    | docker exec -i "$target" mongorestore --archive --drop --quiet
  echo "Copied."
}

collection_count() {
  docker exec "${PROJECT}-mongo-1" mongosh --quiet biopipe \
    --eval 'db.getCollectionNames().length' 2>/dev/null | tr -dc '0-9'
}

# Infrastructure first, so the database is seeded before api and worker can
# write to it. --wait blocks on the healthchecks, and mongo's self-initiates
# the replica set.
compose up -d --wait mongo redis

if [ "$MODE" = "reseed" ]; then
  seed_mongo
elif [ "$(collection_count)" = "0" ]; then
  seed_mongo
else
  echo "This stack already has a database; leaving it alone (--reseed to refresh)."
fi

compose up -d --build api web worker

echo ""
echo "  project  $PROJECT"
echo "  branch   $BRANCH"
echo "  UI       http://localhost:${WT_WEB_PORT}"
echo "  API      http://localhost:${WT_API_PORT}/docs"
echo ""
echo "The main stack on 5173 is untouched. Tear this one down with:"
echo "  ./ops/worktree-up.sh --down"
