#!/usr/bin/env bash
set -euo pipefail

# Install the BioFlow e2e harness as a Hermes desktop plugin + backend.
# Idempotent: safe to re-run. Symlinks are absolute so they survive cwd changes.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
DESKTOP_DIR="$HERMES_HOME/desktop-plugins/bioflow-e2e"
BACKEND_DIR="$HERMES_HOME/plugins/bioflow-e2e/dashboard"
DATA_DIR="$HERMES_HOME/plugins/bioflow-e2e/data"

DRY_RUN=false
if [ "${1:-}" = "--dry-run" ]; then DRY_RUN=true; fi

link() { # link <src> <dst>
  if $DRY_RUN; then echo "would link  $1 -> $2"; return; fi
  mkdir -p "$(dirname "$2")"
  ln -sfn "$1" "$2"
  echo "linked      $2"
}

write_manifest() { # write_manifest <path>
  if $DRY_RUN; then echo "would write $1"; return; fi
  mkdir -p "$(dirname "$1")"
  printf '{"name":"bioflow-e2e","api":"plugin_api.py"}\n' > "$1"
  echo "wrote       $1"
}

mkdir_data() { # mkdir_data <path>
  if $DRY_RUN; then echo "would mkdir $1"; return; fi
  mkdir -p "$1"
  echo "created     $1"
}

link "$SCRIPT_DIR/desktop/plugin.js" "$DESKTOP_DIR/plugin.js"
write_manifest "$BACKEND_DIR/manifest.json"
link "$SCRIPT_DIR/backend/plugin_api.py" "$BACKEND_DIR/plugin_api.py"
mkdir_data "$DATA_DIR"

echo "done."
