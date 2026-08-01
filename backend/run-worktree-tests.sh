#!/usr/bin/env bash
# Run this worktree's tests against the running stack, on a Mongo of our own.
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

# The image to run the tests in.
#
# Mounting this worktree's source fixes half the problem; the other half is the
# image, and it bites exactly when a change adds a dependency. `biopipe-api` is
# built from *main's* Dockerfile, so a branch that installs a new tool runs its
# tests in an image without that tool -- every probe reports it missing, every
# availability path is untested, and nothing says why. That is a nastier
# version of the source problem this script exists to solve, because the
# failures look like real ones.
#
# Measured while adding featureCounts and PyDESeq2: against `biopipe-api` the
# tool-cache test failed with both tools unfingerprintable, because neither was
# installed in the image being tested.
#
# So: prefer this worktree's own stack image when it has been built (by
# ops/worktree-up.sh), and fall back to main's otherwise -- a worktree that
# changes no dependencies needs no stack of its own, and requiring one would
# make the common case slower for a problem it does not have.
BRANCH="$(git -C "$REPO_ROOT" branch --show-current 2>/dev/null || true)"
[ -n "$BRANCH" ] || BRANCH="$(basename "$REPO_ROOT")"
SLUG="$(printf '%s' "$BRANCH" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9_-' '-' | sed 's/^-*//; s/-*$//')"
WT_IMAGE="biopipe-wt-${SLUG}-api"

if docker image inspect "$WT_IMAGE" >/dev/null 2>&1; then
  IMAGE="$WT_IMAGE"
else
  IMAGE="biopipe-api"
fi
echo "Testing in image: $IMAGE" >&2

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

# A private Mongo, not the stack's.
#
# `conftest.py` hardcodes the database name `biopipe_test` and drops every
# collection at session start, so two runs sharing one Mongo wipe each other's
# data mid-test. That surfaces as a rotating handful of DB-touching tests
# failing -- test_mate_link, test_read_pairing, test_variant_taxid -- a
# different set each run, every one of them passing in isolation. Measured on
# one unchanged tree while the stack's own api container was also running
# tests: 7 failed, then 1872 passed, then 5 failed. With a private replica set:
# five consecutive full runs, identical counts.
#
# Redis is still shared: nothing in the suite flushes it, so it does not have
# the same problem.
MONGO_NAME="wt-mongo-$$"

cleanup() { docker rm -f "$MONGO_NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker run -d --rm --name "$MONGO_NAME" --network biopipe_default \
  mongo:7 --replSet rs0 --bind_ip_all >/dev/null

# The driver will not accept writes until the set is initiated.
until docker exec "$MONGO_NAME" mongosh --quiet --eval \
      'try { rs.status().ok } catch (e) { rs.initiate({_id:"rs0",members:[{_id:0,host:"'"$MONGO_NAME"':27017"}]}).ok }' \
      2>/dev/null | grep -q 1; do
  sleep 0.5
done

docker run --rm \
  --network biopipe_default \
  -v "$REPO_ROOT/backend/app:/srv/app" \
  -v "$REPO_ROOT/backend/tests:/srv/tests" \
  -v "$DATA_SOURCE:/data" \
  -w /srv \
  -e MONGO_URL="mongodb://$MONGO_NAME:27017/?replicaSet=rs0" \
  -e REDIS_URL="redis://redis:6379/0" \
  "$IMAGE" python -m pytest "${@:-tests/}" -q
