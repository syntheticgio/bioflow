#!/usr/bin/env bash
set -euo pipefail

# Install the BioFlow e2e harness as a Hermes desktop plugin + backend.
# Idempotent: safe to re-run. The Python backend is COPIED (not symlinked)
# because Hermes refuses to import an api file that resolves outside its
# dashboard/ directory (GHSA-5qr3-c538-wm9j). The frontend plugin.js is
# symlinked so edits hot-reload.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
DESKTOP_DIR="$HERMES_HOME/desktop-plugins/bioflow-e2e"
PLUGIN_DIR="$HERMES_HOME/plugins/bioflow-e2e"
DASHBOARD_DIR="$PLUGIN_DIR/dashboard"
DATA_DIR="$PLUGIN_DIR/data"

DRY_RUN=false
if [ "${1:-}" = "--dry-run" ]; then DRY_RUN=true; fi

say()  { echo "$*"; }
link() { # link <src> <dst>
  if $DRY_RUN; then say "would link  $1 -> $2"; return; fi
  mkdir -p "$(dirname "$2")"
  ln -sfn "$1" "$2"
  say "linked      $2"
}
write_manifest() {
  if $DRY_RUN; then say "would write $1"; return; fi
  mkdir -p "$(dirname "$1")"
  printf '{"name":"bioflow-e2e","api":"plugin_api.py"}\n' > "$1"
  say "wrote       $1"
}
copy_file() { # copy_file <src> <dst>
  if $DRY_RUN; then say "would copy  $1 -> $2"; return; fi
  mkdir -p "$(dirname "$2")"
  rm -f "$2"
  cp "$1" "$2"
  say "copied      $1 -> $2"
}
copy_dir() { # copy_dir <src> <dst>
  if $DRY_RUN; then say "would copy  $1/ -> $2/"; return; fi
  rm -rf "$2"
  mkdir -p "$2"
  cp -R "$1/." "$2/"
  find "$2" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
  say "copied      $1/ -> $2/"
}
mkdir_data() {
  if $DRY_RUN; then say "would mkdir $1"; return; fi
  mkdir -p "$1"
  say "created     $1"
}

# Frontend (symlink: hot-reloads on edit).
link "$SCRIPT_DIR/desktop/plugin.js" "$DESKTOP_DIR/plugin.js"

# Backend (copied: must resolve inside the dashboard/ directory).
write_manifest "$DASHBOARD_DIR/manifest.json"
copy_file "$SCRIPT_DIR/plugin_api.py" "$DASHBOARD_DIR/plugin_api.py"
copy_dir  "$SCRIPT_DIR/backend" "$DASHBOARD_DIR/e2e_backend"

# Test definitions and fixtures, copied next to the plugin root so the shim's
# _PLUGIN_ROOT/tests and _PLUGIN_ROOT/fixtures resolve.
copy_dir  "$SCRIPT_DIR/tests" "$PLUGIN_DIR/tests"
copy_dir  "$SCRIPT_DIR/fixtures" "$PLUGIN_DIR/fixtures"

mkdir_data "$DATA_DIR"

say "done."
