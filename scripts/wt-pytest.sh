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

# A private Mongo, not the stack's.
#
# `conftest.py` hardcodes the database name `biopipe_test` and drops every
# collection at session start. Two concurrent runs against one Mongo therefore
# wipe each other's data mid-test, which surfaces as a rotating handful of
# DB-touching tests failing (test_mate_link, test_read_pairing,
# test_variant_taxid) -- a different set each run, passing in isolation.
#
# Measured: the same tree gave 7 failed, then 1872 passed, then 5 failed on
# three consecutive runs while the stack's own `api` container was also
# running tests. The database name cannot be overridden from outside, but
# `mongo_url` comes from settings, so a throwaway replica-set of our own
# removes the contention entirely.
MONGO_NAME="wt-pytest-mongo-$$"

cleanup() { docker rm -f "$MONGO_NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker run -d --rm --name "$MONGO_NAME" --network biopipe_default \
  mongo:7 --replSet rs0 --bind_ip_all >/dev/null

# The app's driver needs the set initiated before it will accept writes.
until docker exec "$MONGO_NAME" mongosh --quiet --eval \
      'try { rs.status().ok } catch (e) { rs.initiate({_id:"rs0",members:[{_id:0,host:"'"$MONGO_NAME"':27017"}]}).ok }' \
      2>/dev/null | grep -q 1; do
  sleep 0.5
done

docker run --rm \
  --network biopipe_default \
  $(docker inspect biopipe-api-1 --format '{{range .Config.Env}}-e {{.}} {{end}}') \
  -e "MONGO_URL=mongodb://$MONGO_NAME:27017/biopipe?replicaSet=rs0&directConnection=true" \
  -v /Volumes/ModelExtension/BioinfoHelper:/data \
  -v "$WT/backend/app:/srv/app:ro" \
  -v "$WT/backend/tests:/srv/tests:ro" \
  -w /srv \
  biopipe-api \
  python -m pytest "${@:-tests/ -q}"
