#!/usr/bin/env bash
# Migrates BIOINFO_HOME to a new location for the non-launcher (plain
# `docker compose`) case -- this repo's own dev-trunk setup included. See
# docs/superpowers/specs/2026-08-07-bioinfo-home-storage-migration-design.md
# for the full design; this mirrors the launcher's own migration flow
# (scan, space-check, copy, validate, .env update, cleanup) step for step,
# without a GUI.
#
# Usage:
#   ./ops/migrate-storage.sh <new-path> [--keep-original] [--verify-hash]
set -euo pipefail

# Flat margin required at the destination beyond the source's own size --
# matches MIGRATION_SPACE_MARGIN_BYTES in launcher/src-tauri/src/migrate.rs.
# Kept as a duplicated constant rather than a shared file: this is a bash
# script and that is Rust, and the two runtimes have no shared config file
# to source from without inventing one for a single number.
MARGIN_BYTES=$((100 * 1024 * 1024 * 1024))

if [ $# -lt 1 ]; then
  echo "Usage: $0 <new-path> [--keep-original] [--verify-hash]" >&2
  exit 1
fi

NEW_PATH="$1"
shift
KEEP_ORIGINAL=false
VERIFY_HASH=false
for arg in "$@"; do
  case "$arg" in
    --keep-original) KEEP_ORIGINAL=true ;;
    --verify-hash) VERIFY_HASH=true ;;
    *) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"

if [ ! -f "$ENV_FILE" ]; then
  echo "No .env at $ENV_FILE. This script operates on the main checkout's own stack." >&2
  exit 1
fi

CURRENT_PATH="$(grep '^BIOINFO_HOME=' "$ENV_FILE" | cut -d= -f2-)"
if [ -z "$CURRENT_PATH" ]; then
  echo "BIOINFO_HOME not found in $ENV_FILE" >&2
  exit 1
fi

if [ "$CURRENT_PATH" = "$NEW_PATH" ]; then
  echo "New path is the same as the current path ($CURRENT_PATH); nothing to do." >&2
  exit 1
fi

# Refuse while the stack is running -- copying files a container may have
# open is unsafe, mirroring the launcher's LauncherState::Stopped gate.
RUNNING="$(docker compose -p biopipe --project-directory "$REPO_ROOT" ps --status running -q 2>/dev/null || true)"
if [ -n "$RUNNING" ]; then
  echo "The stack is currently running. Stop it first:" >&2
  echo "  docker compose -p biopipe --project-directory $REPO_ROOT down" >&2
  exit 1
fi

if [ ! -d "$CURRENT_PATH" ]; then
  echo "Current BIOINFO_HOME ($CURRENT_PATH) does not exist or is not a directory." >&2
  exit 1
fi

echo "Scanning $CURRENT_PATH..."
SOURCE_BYTES="$(du -sk "$CURRENT_PATH" | cut -f1)"
SOURCE_BYTES=$((SOURCE_BYTES * 1024))
echo "Source size: $((SOURCE_BYTES / 1024 / 1024 / 1024)) GB"

mkdir -p "$NEW_PATH"
AVAILABLE_BYTES="$(df -k "$NEW_PATH" | tail -1 | awk '{print $4}')"
AVAILABLE_BYTES=$((AVAILABLE_BYTES * 1024))
NEEDED_BYTES=$((SOURCE_BYTES + MARGIN_BYTES))

if [ "$AVAILABLE_BYTES" -lt "$NEEDED_BYTES" ]; then
  echo "Not enough free space at $NEW_PATH." >&2
  echo "  Needed:    $((NEEDED_BYTES / 1024 / 1024 / 1024)) GB (source + 100GB margin)" >&2
  echo "  Available: $((AVAILABLE_BYTES / 1024 / 1024 / 1024)) GB" >&2
  exit 1
fi

echo "Copying $CURRENT_PATH -> $NEW_PATH ..."
# -a: archive mode (preserves permissions, symlinks, timestamps).
# --info=progress2: aggregate progress output, matching the launcher
# dialog's bytes/total/percentage display as closely as rsync allows.
if command -v rsync >/dev/null 2>&1; then
  rsync -a --info=progress2 "$CURRENT_PATH"/ "$NEW_PATH"/
else
  cp -a "$CURRENT_PATH"/. "$NEW_PATH"/
fi

echo "Validating copy..."
SOURCE_COUNT="$(find "$CURRENT_PATH" -type f | wc -l | tr -d ' ')"
DEST_COUNT="$(find "$NEW_PATH" -type f | wc -l | tr -d ' ')"
DEST_BYTES_KB="$(du -sk "$NEW_PATH" | cut -f1)"
DEST_BYTES=$((DEST_BYTES_KB * 1024))

if [ "$SOURCE_COUNT" != "$DEST_COUNT" ] || [ "$SOURCE_BYTES" != "$DEST_BYTES" ]; then
  echo "Validation FAILED: file count or size does not match." >&2
  echo "  Source: $SOURCE_COUNT files, $SOURCE_BYTES bytes" >&2
  echo "  Dest:   $DEST_COUNT files, $DEST_BYTES bytes" >&2
  echo "The original at $CURRENT_PATH has NOT been touched." >&2
  exit 1
fi

if [ "$VERIFY_HASH" = true ]; then
  echo "Validating by hash (this may take hours depending on the size of the data)..."
  if ! diff -rq "$CURRENT_PATH" "$NEW_PATH" >/tmp/migrate-storage-diff.$$ 2>&1; then
    echo "Validation FAILED: contents differ between source and destination." >&2
    cat /tmp/migrate-storage-diff.$$ >&2
    rm -f /tmp/migrate-storage-diff.$$
    echo "The original at $CURRENT_PATH has NOT been touched." >&2
    exit 1
  fi
  rm -f /tmp/migrate-storage-diff.$$
fi

echo "Validation passed."

# Rewrite BIOINFO_HOME in .env, preserving every other line.
TMP_ENV="$(mktemp)"
awk -v new="$NEW_PATH" '
  /^BIOINFO_HOME=/ { print "BIOINFO_HOME=" new; next }
  { print }
' "$ENV_FILE" > "$TMP_ENV"
mv "$TMP_ENV" "$ENV_FILE"
echo "Updated BIOINFO_HOME in $ENV_FILE"

if [ "$KEEP_ORIGINAL" = false ]; then
  rm -rf "$CURRENT_PATH"
  echo "Removed original directory: $CURRENT_PATH"
else
  echo "Original directory kept at $CURRENT_PATH (--keep-original)"
fi

echo ""
echo "Migration complete. Start the stack with:"
echo "  docker compose -p biopipe --project-directory $REPO_ROOT up -d --build api web worker"
