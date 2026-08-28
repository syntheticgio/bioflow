#!/usr/bin/env bash
# Probes every enrolled compute node to establish whether it reads the
# primary's BIOINFO_HOME, and records the answer. This is the migration for a
# deployment whose nodes were enrolled before BioFlow recorded that fact:
# they read as *never probed*, and the placement rules that are about to ship
# treat never-probed as not-shared. See
# docs/superpowers/specs/2026-08-25-node-storage-migration-design.md for the
# design; this script is a thin wrapper over POST /nodes/storage-check, which
# owns all of the logic.
#
# Expect the first run to migrate nothing and to report every node as needing
# a storage path. That is correct, not a failure: the primary never recorded
# where any node's storage lives, and a path cannot be guessed -- probing the
# wrong directory would answer "not shared" confidently and wrongly. Supply
# the paths and run again; the second run is the one that migrates.
#
# Safe to re-run at any time. It holds no state and re-probes everything, so
# running it after a share is unmounted records the new answer rather than
# the old one.
#
# Usage:
#   ./ops/check-node-storage.sh [node-id=/path ...]
#
# Examples:
#   ./ops/check-node-storage.sh
#   ./ops/check-node-storage.sh lab-node-1=/data/scratch lab-node-2=/mnt/bio
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_PORT="${API_PORT:-8000}"
API_BASE="${BIOFLOW_API_URL:-http://localhost:${API_PORT}}"

# Build the node-id=/path map from the positional arguments. Rejecting a
# malformed pair rather than ignoring it: a typo'd argument that is silently
# dropped reports the same "needs a path" as supplying nothing, and the
# operator would have no way to tell the two apart.
LOCATIONS_JSON="{}"
for arg in "$@"; do
  case "$arg" in
    -h|--help)
      grep '^# ' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *=/*)
      node_id="${arg%%=*}"
      path="${arg#*=}"
      if [ -z "$node_id" ]; then
        echo "Malformed argument '$arg': expected <node-id>=/absolute/path" >&2
        exit 1
      fi
      LOCATIONS_JSON="$(NODE_ID="$node_id" NODE_PATH="$path" CURRENT="$LOCATIONS_JSON" \
        python3 -c 'import json,os; d=json.loads(os.environ["CURRENT"]); d[os.environ["NODE_ID"]]=os.environ["NODE_PATH"]; print(json.dumps(d))')"
      ;;
    *)
      echo "Unknown argument '$arg': expected <node-id>=/absolute/path" >&2
      echo "  ./ops/check-node-storage.sh lab-node-1=/data/scratch" >&2
      exit 1
      ;;
  esac
done

# --- preconditions, before anything is sent -------------------------------

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required to build the request and format the report." >&2
  exit 1
fi

RUNNING="$(docker compose -p biopipe --project-directory "$REPO_ROOT" ps --status running -q api 2>/dev/null || true)"
if [ -z "$RUNNING" ]; then
  echo "The API is not running, so there is nothing to ask. Start the stack:" >&2
  echo "  docker compose up -d" >&2
  exit 1
fi

if ! python3 -c "
import sys, urllib.request
try:
    urllib.request.urlopen('${API_BASE}/healthz', timeout=5)
except Exception as e:
    sys.stderr.write(str(e) + '\n')
    sys.exit(1)
" 2>/dev/null; then
  echo "The API at ${API_BASE} did not answer, though its container is up." >&2
  echo "Check what it is reporting:" >&2
  echo "  docker compose logs --tail 50 api" >&2
  exit 2
fi

# --- the sweep ------------------------------------------------------------

echo "Probing every enrolled node's storage via ${API_BASE}..."
echo "An unreachable node costs 20 seconds before the sweep moves on."
echo

# The exit code carries the outcome, so the report is written to stdout and
# the script's own failures to stderr.
set +e
BIOFLOW_API_BASE="$API_BASE" LOCATIONS_JSON="$LOCATIONS_JSON" python3 - <<'PY'
import json
import os
import sys
import urllib.error
import urllib.request

base = os.environ["BIOFLOW_API_BASE"]
body = json.dumps({"storage_locations": json.loads(os.environ["LOCATIONS_JSON"])})

req = urllib.request.Request(
    f"{base}/api/v1/nodes/storage-check",
    data=body.encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    # Generous: the sweep is N SSH connects in sequence, and an offline node
    # spends 20s of that before it is given up on.
    with urllib.request.urlopen(req, timeout=600) as resp:
        result = json.load(resp)
except urllib.error.HTTPError as e:
    sys.stderr.write(f"The sweep failed ({e.code}): {e.read().decode(errors='replace')}\n")
    sys.exit(3)
except Exception as e:
    sys.stderr.write(f"The sweep failed: {e}\n")
    sys.exit(3)

LABELS = {
    "shared": "SHARED",
    "not_shared": "NOT SHARED",
    "unreachable": "CANNOT CHECK",
    "not_probeable": "CANNOT CHECK",
    "no_recorded_path": "NEEDS A PATH",
}

nodes = result.get("nodes", [])
for node in nodes:
    print(f"{LABELS.get(node['outcome'], node['outcome']):<13} {node['node_id']}")
    print(f"              {node['detail']}")
    print()

checked = result.get("checked", 0)
total = result.get("total", 0)
print(f"Probed {checked} of {total} node(s).")

needs_path = [n["node_id"] for n in nodes if n["outcome"] == "no_recorded_path"]
not_shared = [n for n in nodes if n["outcome"] == "not_shared"]

# End by printing the next command, whatever the outcome asks for.
if needs_path:
    print()
    print("Supply the storage path each of these nodes uses and run again:")
    args = " ".join(f"{n}=/data/scratch" for n in needs_path)
    print(f"  ./ops/check-node-storage.sh {args}")
    print("(substituting each node's real path -- the one above is only a shape.)")

if not_shared:
    print()
    print("These nodes do not read the primary's storage. Until they do, they")
    print("can only run work that fetches its own inputs:")
    for n in not_shared:
        print(f"  {n['node_id']}: mount the primary's BIOINFO_HOME at {n['storage_location']}")

if not needs_path and not not_shared:
    print()
    print("Every node that could be probed reads the primary's storage.")

# 0 only when the fleet is fully established. A run that still has questions
# outstanding exits non-zero so a caller in a script can tell.
sys.exit(0 if not needs_path and not not_shared else 4)
PY
STATUS=$?
set -e
exit "$STATUS"
