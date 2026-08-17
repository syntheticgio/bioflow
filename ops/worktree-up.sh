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
#         ./ops/worktree-up.sh --list      # every worktree stack on this machine
#         ./ops/worktree-up.sh --prune     # tear down the orphaned ones
#         ./ops/worktree-up.sh --prune --dry-run   # show what --prune would do
#
# --list and --prune are machine-wide rather than about *this* worktree, and
# unlike everything else here they run from anywhere -- including the main
# checkout. They have to: the case they exist for is a stack whose worktree
# directory has already been deleted, which leaves no worktree to run --down
# from and no obvious way to name the stack. See #321.
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

# The project-name prefix every worktree stack shares. `--list` and `--prune`
# below identify stacks by this rather than by anything about the directory
# they are run from.
WT_PREFIX="${MAIN_PROJECT}-wt-"

# Every worktree stack that exists on this machine, one project name per line.
#
# Reads container labels rather than `docker compose ls`, because a stack whose
# containers are all stopped does not appear in `compose ls` without `-a`, and a
# stopped orphan is exactly what this is looking for -- it still holds volumes
# and still confuses the next person who finds it.
all_wt_projects() {
  docker ps -a \
    --filter "label=com.docker.compose.project" \
    --format '{{.Label "com.docker.compose.project"}}' 2>/dev/null \
    | grep "^${WT_PREFIX}" \
    | sort -u
}

# Every slug a live worktree currently occupies.
#
# `git worktree list` is the authority for what still exists. Slugs are derived
# the same way the `up` path derives its own above, so the comparison is
# slug-to-slug rather than path-to-path -- a worktree can be moved on disk
# without changing which stack it owns.
#
# Both of the up path's slug sources are covered, and the second one matters:
# `up` uses the branch name, but falls back to `basename "$WT_ROOT"` when
# `git branch --show-current` is empty, which is exactly what a **detached
# HEAD** worktree produces. Such a worktree emits no `branch` line in
# `--porcelain` output at all, so deriving slugs from branches alone would make
# every live detached worktree look orphaned -- and --prune would delete a
# stack somebody is actively using. Real case, not hypothetical: two of the
# worktrees on the machine this was written on are detached.
#
# Emitting both candidate slugs per worktree is deliberate. They are only ever
# used to *spare* a stack from pruning, so an extra slug can at worst leave an
# orphan behind for the next run -- while a missing one deletes live work.
#
# `git worktree list` also reports the main checkout itself, which is harmless:
# neither of its slugs can produce a `biopipe-wt-` project to match against.
# Note the trailing newline: callers below consume this as a line-per-slug
# stream and match it with `grep -qxF`. Without it every slug would run
# together into one unmatchable line -- which fails toward "everything looks
# orphaned", i.e. toward deleting live stacks.
normalize_slug() {
  printf '%s' "$1" \
    | tr '[:upper:]' '[:lower:]' \
    | tr -c 'a-z0-9_-' '-' \
    | sed 's/^-*//; s/-*$//'
  printf '\n'
}

live_slugs() {
  local line path branch
  path=""
  while IFS= read -r line; do
    case "$line" in
      "worktree "*)
        path="${line#worktree }"
        # The basename slug, i.e. what `up` falls back to for a detached HEAD.
        normalize_slug "$(basename "$path")"
        ;;
      "branch "*)
        branch="${line#branch }"
        normalize_slug "${branch#refs/heads/}"
        ;;
    esac
  done < <(git -C "$MAIN_ROOT" worktree list --porcelain 2>/dev/null)
}

# Prints one line per worktree stack: project, slug, ports, and whether its
# worktree still exists. Used by --list, and by --prune to show its plan.
report_stacks() {
  local live
  live="$(live_slugs)"

  local project slug status web api
  while IFS= read -r project; do
    [ -n "$project" ] || continue
    slug="${project#"$WT_PREFIX"}"

    if printf '%s\n' "$live" | grep -qxF "$slug"; then
      status="worktree present"
    else
      status="ORPHANED (no worktree)"
    fi

    # Ports come from HostConfig rather than the runtime port list, which is
    # empty for any container that is not currently running -- and a stopped
    # stack is precisely what this command is for.
    web="$(docker inspect "${project}-web-1" \
      --format '{{range $p, $b := .HostConfig.PortBindings}}{{range $b}}{{.HostPort}}{{end}}{{end}}' \
      2>/dev/null || true)"
    api="$(docker inspect "${project}-api-1" \
      --format '{{range $p, $b := .HostConfig.PortBindings}}{{range $b}}{{.HostPort}}{{end}}{{end}}' \
      2>/dev/null || true)"

    printf '  %-46s %-22s UI:%-6s API:%-6s\n' \
      "$project" "$status" "${web:-?}" "${api:-?}"
  done <<< "$(all_wt_projects)"
}

# Tear down one stack by project name, from wherever this is running.
#
# `down -v` needs a compose file to resolve services, but not *this* worktree's
# -- the main checkout's copy describes the same services, and the project name
# is what actually selects which containers and volumes are removed. That is
# what makes this work for a stack whose own worktree is gone.
down_project() {
  local project="$1"

  # The guard that matters most here. --prune acts on a set rather than one
  # named stack, so a bug in the filter could otherwise reach the main stack.
  if [ "$project" = "$MAIN_PROJECT" ] || [ "${project#"$WT_PREFIX"}" = "$project" ]; then
    echo "Refusing to act on '$project': not a worktree stack." >&2
    return 1
  fi

  COMPOSE_PROJECT_NAME="$project" docker compose \
    --project-directory "$MAIN_ROOT" \
    -f "$MAIN_ROOT/docker-compose.yml" \
    -f "$MAIN_ROOT/ops/docker-compose.worktree.yml" \
    down -v
}

# --list and --prune are dispatched here, before the worktree-specific setup
# below (branch slug, .env, port probing) that neither needs and that would
# refuse to run in the main checkout.
case "${1:-}" in
  --list)
    if [ -z "$(all_wt_projects)" ]; then
      echo "No worktree stacks on this machine."
      exit 0
    fi
    echo "Worktree stacks:"
    report_stacks
    echo ""
    echo "Orphaned stacks can be removed with: ./ops/worktree-up.sh --prune"
    exit 0
    ;;
  --prune)
    DRY_RUN="no"
    case "${2:-}" in
      --dry-run) DRY_RUN="yes" ;;
      "")        ;;
      *)         echo "Unknown option: $2 (expected --dry-run)" >&2; exit 1 ;;
    esac

    LIVE="$(live_slugs)"
    ORPHANS=""
    while IFS= read -r project; do
      [ -n "$project" ] || continue
      slug="${project#"$WT_PREFIX"}"
      printf '%s\n' "$LIVE" | grep -qxF "$slug" && continue
      ORPHANS="${ORPHANS}${project}"$'\n'
    done <<< "$(all_wt_projects)"

    ORPHANS="$(printf '%s' "$ORPHANS" | sed '/^$/d')"
    if [ -z "$ORPHANS" ]; then
      echo "No orphaned worktree stacks; nothing to prune."
      exit 0
    fi

    echo "Orphaned worktree stacks (no worktree directory):"
    printf '%s\n' "$ORPHANS" | sed 's/^/  /'
    echo ""

    if [ "$DRY_RUN" = "yes" ]; then
      echo "--dry-run: nothing removed."
      exit 0
    fi

    # `down -v` deletes each stack's volumes. Bounded -- a worktree stack's
    # database is a snapshot of the main stack's, not unique data -- but it is
    # a set-valued destructive operation, so it says what it will do and asks
    # first. A non-interactive run (no TTY, e.g. CI) declines rather than
    # assuming yes.
    if [ -t 0 ]; then
      printf 'Remove these stacks and their volumes? [y/N] '
      read -r reply
    else
      reply="n"
      echo "Not an interactive terminal; declining. Re-run from a terminal to confirm."
    fi

    case "$reply" in
      [yY]|[yY][eE][sS]) ;;
      *) echo "Nothing removed."; exit 0 ;;
    esac

    while IFS= read -r project; do
      [ -n "$project" ] || continue
      echo "Removing $project..."
      down_project "$project" || echo "  failed; continuing" >&2
    done <<< "$ORPHANS"

    echo "Done."
    exit 0
    ;;
esac

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

# Let the stack report which revision it is serving (#452).
#
# docker-compose.override.yml mounts this worktree's `./.git`, which for a
# linked worktree is a file reading `gitdir: <GIT_COMMON>/worktrees/<name>`.
# That is a host path, so inside the container it dangles unless the main
# checkout's .git is mounted at the identical path -- which is what these two
# feed into ops/docker-compose.worktree.yml. The source and the target are
# deliberately the same string: the pointer file's contents are fixed, so the
# container path is not ours to choose.
#
# In a non-worktree checkout `.git` is already a real directory and the
# override's own mount is sufficient, so this maps /dev/null onto itself --
# a no-op bind rather than a special case in the compose file.
if [ -d "$GIT_COMMON" ] && [ "$WT_ROOT" != "$MAIN_ROOT" ]; then
  export WT_MAIN_GIT="$GIT_COMMON"
  export WT_MAIN_GIT_PATH="$GIT_COMMON"
else
  export WT_MAIN_GIT="/dev/null"
  export WT_MAIN_GIT_PATH="/dev/null"
fi

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
  # --list and --prune never reach here; they are dispatched above, before the
  # worktree-specific setup they do not need.
  *)        echo "Unknown option: $1 (expected --down, --reseed, --list or --prune)" >&2; exit 1 ;;
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
