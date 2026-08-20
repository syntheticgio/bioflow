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
#         ./backend/run-worktree-tests.sh --with-sshd tests/integration
#         ./backend/run-worktree-tests.sh --with-node tests/integration
set -euo pipefail

# --with-sshd: also run a real sshd for tests/integration/test_node_ssh_live.py.
#
# Those tests skip themselves unless BIOFLOW_TEST_SSHD_HOST is set, because the
# test container has no Docker socket and so cannot start sshd itself. Opt-in
# rather than always-on: it pulls an image and adds ~10s of startup that the
# other ~1900 tests have no use for.
#
# --with-node: a superset, for tests/integration/test_node_update_live.py. The
# same sshd, plus a Docker CLI and the host's Docker socket, plus a compose
# file at the path node_update_service.py hardcodes. That makes it a stand-in
# for an enrolled compute node: `docker compose pull` and `up -d` run for real
# against a real daemon over a real SSH transport.
#
# The socket means the "node's" daemon is really this machine's daemon, so the
# isolation is fiction while the Docker behaviour is not. That is the right
# trade for what these tests assert -- issue #474's check 5 is that `up -d`
# exits 0 for a container that immediately dies, which is a property of how
# run_update reads exit codes, not of whose daemon ran the container.
WITH_SSHD=
WITH_NODE=
case "${1:-}" in
  --with-sshd)
    WITH_SSHD=1
    shift
    ;;
  --with-node)
    WITH_SSHD=1
    WITH_NODE=1
    shift
    ;;
esac

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# The image to run the tests in.
#
# Mounting this worktree's source fixes half the problem; the other half is the
# image, and it bites exactly when a change adds a dependency. The main stack's
# image is built from *main's* Dockerfile, so a branch that installs a new tool
# runs its tests in an image without that tool -- every probe reports it missing, every
# availability path is untested, and nothing says why. That is a nastier
# version of the source problem this script exists to solve, because the
# failures look like real ones.
#
# Measured while adding featureCounts and PyDESeq2: against the main image the
# tool-cache test failed with both tools unfingerprintable, because neither was
# installed in the image being tested.
#
# So: prefer this worktree's own stack image when it has been built (by
# ops/worktree-up.sh), and fall back to main's otherwise -- a worktree that
# changes no dependencies needs no stack of its own, and requiring one would
# make the common case slower for a problem it does not have.
#
# Both halves of that read off the running stack rather than naming a tag,
# because the tags moved once already and did it silently. Before #37 the api
# service carried only `build:`, so Compose auto-tagged builds
# `<project>-<service>` and the names here (`biopipe-api`,
# `biopipe-wt-<slug>-api`) were the built images. #37 added
# `image: ghcr.io/syntheticgio/bioflow-backend:${BIOFLOW_TAG:-latest}`, and a
# service with both `image:` and `build:` still builds from source but tags the
# result with the `image:` name -- so `biopipe-api` stopped being written to
# and simply sat there, days old, with nothing to say so. Every run took the
# fallback path (the stale tag still resolves), and on issue #25 that meant the
# whole API layer failing at import on a missing `cryptography` -- a dependency
# the actually-current build had. Read the fallback off `biopipe-api-1`
# instead: it is by definition the image the stack is running, whatever
# BIOFLOW_TAG says and whatever the tags are called next.
BRANCH="$(git -C "$REPO_ROOT" branch --show-current 2>/dev/null || true)"
[ -n "$BRANCH" ] || BRANCH="$(basename "$REPO_ROOT")"
SLUG="$(printf '%s' "$BRANCH" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9_-' '-' | sed 's/^-*//; s/-*$//')"
# Must match the BIOFLOW_TAG that ops/worktree-up.sh exports.
WT_IMAGE="ghcr.io/syntheticgio/bioflow-backend:wt-${SLUG}"

# One inspect for both the fallback image and the /data mount, so a stopped
# stack is reported once rather than as two unrelated-looking failures.
#
# BIOINFO_HOME must be mounted the same way the real api container mounts it.
# Without it, tests that touch /data (reap_report_dirs and friends) operate on
# a tmpfs the assertions know nothing about and fail for the wrong reason.
read -r STACK_IMAGE DATA_SOURCE <<<"$(docker inspect biopipe-api-1 \
  --format '{{.Config.Image}} {{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' \
  2>/dev/null || true)"

if [ -z "${DATA_SOURCE:-}" ] || [ -z "${STACK_IMAGE:-}" ]; then
  echo "Could not resolve the image and /data mount from biopipe-api-1. Is the stack up?" >&2
  exit 1
fi

if docker image inspect "$WT_IMAGE" >/dev/null 2>&1; then
  IMAGE="$WT_IMAGE"
else
  IMAGE="$STACK_IMAGE"
fi
echo "Testing in image: $IMAGE" >&2

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
SSHD_NAME="wt-sshd-$$"

cleanup() {
  # -v matters: mongo:7 and the sshd image both declare anonymous VOLUMEs, and
  # neither --rm nor `rm -f` removes those -- only `-v` does. Without it every
  # run strands two volumes forever (#719).
  docker rm -fv "$MONGO_NAME" >/dev/null 2>&1 || true
  docker rm -fv "$SSHD_NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker run -d --rm --name "$MONGO_NAME" --network biopipe_default \
  mongo:7 --replSet rs0 --bind_ip_all >/dev/null

# The driver will not accept writes until the set is initiated.
until docker exec "$MONGO_NAME" mongosh --quiet --eval \
      'try { rs.status().ok } catch (e) { rs.initiate({_id:"rs0",members:[{_id:0,host:"'"$MONGO_NAME"':27017"}]}).ok }' \
      2>/dev/null | grep -q 1; do
  sleep 0.5
done

SSHD_ENV=()
if [ -n "$WITH_SSHD" ]; then
  # linuxserver/openssh-server listens on 2222 and enables password auth for
  # the user it creates, which is what the provisioning flow starts from: the
  # user's password is used once, to install BioFlow's own key.
  SSHD_MOUNTS=()
  if [ -n "$WITH_NODE" ]; then
    SSHD_MOUNTS=(-v /var/run/docker.sock:/var/run/docker.sock)
  fi

  docker run -d --rm --name "$SSHD_NAME" --network biopipe_default \
    -e USER_NAME=bioflow -e USER_PASSWORD=testpw -e PASSWORD_ACCESS=true \
    -e PUID=1000 -e PGID=1000 \
    "${SSHD_MOUNTS[@]+"${SSHD_MOUNTS[@]}"}" \
    lscr.io/linuxserver/openssh-server:latest >/dev/null

  # Wait for sshd to actually accept connections rather than sleeping a fixed
  # interval -- the image generates host keys on first boot, which is the slow
  # part and varies by machine.
  echo "Waiting for sshd..." >&2
  for _ in $(seq 60); do
    if docker logs "$SSHD_NAME" 2>&1 | grep -q "sshd is listening on port 2222"; then
      break
    fi
    sleep 0.5
  done

  SSHD_ENV=(
    -e BIOFLOW_TEST_SSHD_HOST="$SSHD_NAME"
    -e BIOFLOW_TEST_SSHD_PORT=2222
    -e BIOFLOW_TEST_SSHD_USER=bioflow
    -e BIOFLOW_TEST_SSHD_PASSWORD=testpw
  )

  if [ -n "$WITH_NODE" ]; then
    echo "Provisioning the node sidecar..." >&2

    # The Docker CLI and the compose plugin, which the base image has no use
    # for and so does not ship.
    docker exec "$SSHD_NAME" apk add --no-cache docker-cli docker-cli-compose \
      >/dev/null 2>&1

    # The socket is bind-mounted with the host's ownership, which is root on
    # the Docker Desktop VM and not the uid this image runs sshd's user as.
    # Widening the mode inside the container is enough and touches nothing on
    # the host, since the mount is a node in the container's own filesystem.
    docker exec "$SSHD_NAME" chmod 666 /var/run/docker.sock

    # `run_update` hardcodes INSTALL_DIR=~/.bioflow and always names
    # `-f ~/.bioflow/docker-compose.yml`, so the path is part of the contract
    # under test rather than something the test may choose.
    #
    # The worker image is alpine running /bin/true: it pulls, it starts, and
    # it exits 0 immediately. `docker compose up -d` still exits 0 -- which is
    # exactly the condition issue #474's check 5 exists to catch, and the
    # reason the verify phase cannot trust the restart phase's exit status.
    #
    # HOME is set explicitly: `docker exec -u bioflow` does not read the
    # user's passwd entry, so `~` would expand to /root and the write would
    # fail on permissions. The SSH session run_update opens *does* get
    # /config, so this is what makes the two agree on where ~/.bioflow is.
    docker exec -u bioflow -e HOME=/config "$SSHD_NAME" sh -c '
      mkdir -p ~/.bioflow &&
      cat > ~/.bioflow/docker-compose.yml <<YAML
services:
  worker:
    image: alpine:3.20
    command: ["/bin/true"]
YAML
    '

    SSHD_ENV+=(-e BIOFLOW_TEST_NODE=1)
  fi
fi

# Quiet by default, but never on top of a verbosity flag the caller passed.
#
# This used to be an unconditional `-q` appended after "$@", and pytest's
# verbosity is additive, so it corrupted both documented invocations above:
#
#   ...tests/ -q   ->  pytest -q -q  ->  *double* quiet, which drops the
#                      "NNNN passed in Xs" summary line entirely while still
#                      exiting 0. CLAUDE.md's commit rule is "read the count,
#                      not the exit code", and the count was unreadable.
#   ...tests/ -v   ->  pytest -v -q  ->  nets to zero, i.e. not verbose either.
#
# So only default the verbosity when the caller expressed no opinion.
PYTEST_ARGS=("$@")
if [ "${#PYTEST_ARGS[@]}" -eq 0 ]; then
  PYTEST_ARGS=(tests/)
fi

has_verbosity=
for arg in "${PYTEST_ARGS[@]}"; do
  case "$arg" in
    -q | -qq | --quiet | -v | -vv | -vvv | --verbose | --verbosity=*) has_verbosity=1 ;;
  esac
done
[ -n "$has_verbosity" ] || PYTEST_ARGS+=(-q)

# Parallel by default, in two phases: everything but `heavy` across
# PYTEST_WORKERS workers, then the heavy-marked tests alone. Same reasoning as
# the Makefile's -- see the PYTEST_WORKERS comment there for why 8 rather than
# `auto`, which would size the run by CPU count (24) while memory (12.4 GB) is
# what is actually scarce.
#
# Lower than the Makefile's default would be defensible here, since several
# worktree runs can be in flight at once, but they are already separated: each
# gets its own Mongo (below) and its own test databases (per-run token, #679).
# 8 stays for one reason -- an agent waiting on a worktree run is the case this
# script exists to serve, and the measured cost is ~2.4 GB.
WORKERS="${PYTEST_WORKERS:-8}"

# Unless the caller is steering execution themselves. `-m` matters as much as
# `-n`: a caller selecting a marker may be deliberately asking for the heavy
# tests, and splitting their selection into two phases would run something
# they did not ask for and drop something they did.
CALLER_CONTROLS=
for arg in "${PYTEST_ARGS[@]}"; do
  case "$arg" in
    -n | -n* | --numprocesses | --numprocesses=* | -m | -m* | --dist | --dist=* | -p)
      CALLER_CONTROLS=1
      ;;
  esac
done

# The interpreter is named by absolute path, never as bare `python`: the image
# puts a tool venv (/opt/medaka/env/bin) ahead of the app interpreter on PATH,
# so both `python` and `python3` resolve to an environment with none of the
# app's dependencies in it -- "No module named pytest", from an image where
# pytest is demonstrably installed.
#
# --cpus bounds the run so several agents' worktree suites cannot saturate the
# host between them. It is deliberately a little above WORKERS: the workers are
# the parallel part, but the controller process and Mongo's client threads want
# time too, and pinning it exactly to WORKERS makes the run slower than the
# same worker count without a limit.
run_pytest() {
  docker run --rm \
    --network biopipe_default \
    --cpus "$((WORKERS + 2))" \
    -v "$REPO_ROOT/backend/app:/srv/app" \
    -v "$REPO_ROOT/backend/tests:/srv/tests" \
    -v "$REPO_ROOT/VERSION:/VERSION:ro" \
    -v "$REPO_ROOT/docker-compose.override.yml:/docker-compose.override.yml:ro" \
    -v "$REPO_ROOT/backend/pi-skills:/backend/pi-skills:ro" \
    -v "$DATA_SOURCE:/data" \
    -w /srv \
    -e MONGO_URL="mongodb://$MONGO_NAME:27017/?replicaSet=rs0" \
    -e REDIS_URL="redis://redis:6379/0" \
    ${BIOFLOW_TEST_LIVE_DATA:+-e BIOFLOW_TEST_LIVE_DATA="$BIOFLOW_TEST_LIVE_DATA"} \
    "${SSHD_ENV[@]+"${SSHD_ENV[@]}"}" \
    "$IMAGE" /usr/local/bin/python3.12 -m pytest "$@"
}

if [ -n "$CALLER_CONTROLS" ]; then
  run_pytest "${PYTEST_ARGS[@]}"
else
  run_pytest -m "not heavy" -n "$WORKERS" --dist loadgroup "${PYTEST_ARGS[@]}"
  # Exit 5 is "no tests collected", which is what the heavy phase returns
  # while the marker has no members. Tolerated so the split costs nothing
  # until a test earns the mark.
  run_pytest -m heavy "${PYTEST_ARGS[@]}" || [ $? -eq 5 ]
fi
