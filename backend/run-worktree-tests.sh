#!/usr/bin/env bash
# Run this worktree's tests against the running stack's Mongo/Redis.
#
# Why this exists: `docker compose exec api pytest` runs the *main repo's*
# code -- the api container bind-mounts /Users/.../local-bio-pipeliner/backend,
# not this worktree. Per CLAUDE.md we must not repoint the shared stack at a
# worktree (it silently hijacks port 5173 for everyone). So this spins up a
# throwaway container on the same network, mounting this worktree's source.
#
# Usage:  ./backend/run-worktree-tests.sh [pytest args...]
#         ./backend/run-worktree-tests.sh tests/models -v
#         ./backend/run-worktree-tests.sh            # whole suite
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# BIOINFO_HOME must be mounted the same way the real api container mounts it.
# Without it, tests that touch /data (reap_report_dirs and friends) operate on
# a tmpfs the assertions know nothing about and fail for the wrong reason.
# Read the source from the running container so the two can never drift.
DATA_SOURCE="$(docker inspect biopipe-api-1 \
  --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}')"

if [ -z "$DATA_SOURCE" ]; then
  echo "Could not resolve the /data mount from biopipe-api-1. Is the stack up?" >&2
  exit 1
fi

exec docker run --rm \
  --network biopipe_default \
  -v "$REPO_ROOT/backend/app:/srv/app" \
  -v "$REPO_ROOT/backend/tests:/srv/tests" \
  -v "$DATA_SOURCE:/data" \
  -w /srv \
  -e MONGO_URL="mongodb://mongo:27017/?replicaSet=rs0" \
  -e REDIS_URL="redis://redis:6379/0" \
  biopipe-api python -m pytest "${@:-tests/}" -q
