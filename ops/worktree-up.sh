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
# Ports are chosen per worktree: derived from the branch name so a given
# worktree keeps the same URL run to run, then probed upward for a free pair so
# concurrent stacks never collide. The script prints the ones it picked. Set
# WT_WEB_PORT/WT_API_PORT to pin specific ones.
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

# Tag this stack's images apart from the main stack's.
#
# COMPOSE_PROJECT_NAME used to be enough: before #37 the services carried only
# `build:`, so Compose auto-tagged each built image `<project>-<service>` and a
# distinct project name got distinct image tags for free. #37 added
# `image: ghcr.io/syntheticgio/bioflow-*:${BIOFLOW_TAG:-latest}` to
# docker-compose.yml, and when a merged service has both `image:` and `build:`
# Compose builds from source but tags the result with the `image:` name --
# which no longer contains the project name at all. Without this line a
# worktree build would overwrite `:latest`, i.e. the tag the *main* stack
# resolves, so the next `docker compose up -d api` on 5173 without `--build`
# would quietly start serving this branch. That is the same failure this whole
# script exists to prevent, arriving by a different route.
#
# Shell environment outranks --env-file in Compose, so this wins even if the
# main checkout's .env pins BIOFLOW_TAG.
export BIOFLOW_TAG="wt-${SLUG}"

# Pick a port pair this worktree can have to itself.
#
# These used to be fixed at 5273/8100, which meant the *second* concurrent
# worktree stack died on "Bind for 0.0.0.0:8100 failed: port is already
# allocated" unless the user set WT_WEB_PORT/WT_API_PORT by hand. Several
# agents work in this repo at once -- eight worktrees existed when this was
# written -- so that was a routine failure, and the manual workaround left
# stacks on hand-bumped ports nobody could predict.
#
# Derived from the branch slug rather than simply scanning from a base, so a
# given worktree keeps the same URL across restarts: the port you bookmarked
# yesterday is still that worktree's port today. A hash alone is not enough
# though -- over the eight real worktrees at the time, two collided (birthday
# paradox in a 100-wide range is likelier than it sounds) -- so the hash only
# chooses where to *start* looking, and we probe upward from there for a pair
# that is actually free.
#
# An explicit WT_WEB_PORT/WT_API_PORT in the environment still wins, and is
# taken as-is: an override is a deliberate request for a specific port, so
# silently moving it would defeat the point.
port_in_use() {
  # A container publishing it, or anything else on the host holding it.
  # Both matter: the competing listener is usually another worktree stack, but
  # it can equally be an unrelated dev server.
  #
  # This stack's *own* containers are excluded. Re-running the script against a
  # already-running worktree is the normal way to rebuild it, and without this
  # the script would see its own published port, judge it taken, and hand the
  # stack a different URL on every restart -- the opposite of the stability the
  # slug hash exists to provide.
  if docker ps --format '{{.Names}} {{.Ports}}' \
    | grep -v "^${PROJECT}-" \
    | grep -qE "[: ]$1->"; then
    return 0
  fi
  if command -v lsof >/dev/null 2>&1 \
    && lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; then
    # lsof sees the docker-proxy holding this stack's own port too, so fall
    # through to a docker-side check rather than treating it as taken.
    if ! docker ps --format '{{.Names}} {{.Ports}}' \
      | grep "^${PROJECT}-" | grep -qE "[: ]$1->"; then
      return 0
    fi
  fi
  return 1
}

if [ -z "${WT_WEB_PORT:-}" ] || [ -z "${WT_API_PORT:-}" ]; then
  # Offset in [0,99] from the slug, so each worktree starts at its own place.
  OFFSET="$(printf '%s' "$SLUG" | shasum -a 256 | tr -dc '0-9' | tail -c 4)"
  OFFSET=$((10#${OFFSET:-0} % 100))

  for _ in $(seq 0 99); do
    CAND_WEB=$((5200 + OFFSET))
    CAND_API=$((8100 + OFFSET))
    # Both must be free, and both move together, so web and api stay a
    # predictable 2900 apart rather than drifting into an unmemorable pair.
    if ! port_in_use "$CAND_WEB" && ! port_in_use "$CAND_API"; then
      break
    fi
    OFFSET=$(((OFFSET + 1) % 100))
    CAND_WEB="" ; CAND_API=""
  done

  if [ -z "${CAND_WEB:-}" ]; then
    echo "Could not find a free port pair in 5200-5299 / 8100-8199." >&2
    echo "Tear down a worktree stack, or set WT_WEB_PORT and WT_API_PORT." >&2
    exit 1
  fi

  WT_WEB_PORT="$CAND_WEB"
  WT_API_PORT="$CAND_API"
fi
export WT_WEB_PORT WT_API_PORT

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
# write to it. --wait blocks on the healthchecks; the replica set is initiated
# by ops/mongo-init/rs-init.js during the entrypoint's init phase, before
# mongod's real boot, so it is already PRIMARY by the time --wait looks.
#
# This used to fail roughly one run in three with "container ... is unhealthy"
# (#101). rs-init.js was initiating with the member name `mongo:27017`, which
# cannot work during initdb -- the entrypoint pins that temporary mongod to
# `--bind_ip 127.0.0.1`, so it does not map the name to itself -- and the
# error was swallowed. The set was then left for the healthcheck's fallback to
# initiate a few seconds into the real boot, and --wait polls health during
# exactly that window. See rs-init.js for the full mechanism.
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
