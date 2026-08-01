#!/usr/bin/env bash
# Run this worktree's backend tests in a throwaway container.
#
# The shared `biopipe` stack bind-mounts the MAIN repo, so
# `docker compose exec api pytest` runs main's code, not this branch's.
# This mounts the worktree at /srv instead, reusing the already-built
# `biopipe-api` image and the running stack's network and env.
#
# Usage: scripts/wt-pytest.sh [pytest args...]
#   scripts/wt-pytest.sh tests/metadata -q
#   scripts/wt-pytest.sh tests/ -q
set -euo pipefail

WT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

exec docker run --rm \
  --network biopipe_default \
  $(docker inspect biopipe-api-1 --format '{{range .Config.Env}}-e {{.}} {{end}}') \
  -v /Volumes/ModelExtension/BioinfoHelper:/data \
  -v "$WT/backend/app:/srv/app:ro" \
  -v "$WT/backend/tests:/srv/tests:ro" \
  -w /srv \
  biopipe-api \
  python -m pytest "${@:-tests/ -q}"
