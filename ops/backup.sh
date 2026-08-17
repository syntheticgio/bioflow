#!/usr/bin/env bash
#
# Backup and restore the BioFlow research record.
#
# The Mongo database is the irreplaceable half: metadata, provenance, run
# history, timings. It is small. `/data` is large and is *enumerated* rather
# than copied -- see docs/superpowers/specs/2026-08-17-backup-and-restore-design.md
# for why (copying hundreds of gigabytes is rsync's job).
#
# The Fernet key at $BIOINFO_HOME/.biopipe/secret.key is deliberately NEVER
# read by this script. app/services/ai/crypto.py names "a stray mongodump in a
# backup" as the exact threat it defends against; shipping the key beside the
# ciphertext would undo that.
set -euo pipefail

# The Mongo target is an environment variable so tests can redirect it at a
# throwaway container. A test pointed at the real stack drops the user's
# research database. Do not inline this default at the call sites.
MONGO_CONTAINER="${MONGO_CONTAINER:-biopipe-mongo-1}"
MONGO_DB="${MONGO_DB:-biopipe}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log()  { printf '%s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage:
  ops/backup.sh backup                     Take a backup into $BACKUP_DIR
  ops/backup.sh restore <dir> [--force]    Restore a backup (overwrites the database)
  ops/backup.sh verify <dir> [--hash]      Check /data against a backup's manifest

Environment:
  BACKUP_DIR        Where backups land (default: ./backups)
  MONGO_CONTAINER   Mongo container to talk to (default: biopipe-mongo-1)
  MONGO_DB          Database name (default: biopipe)
EOF
}

# --- dispatch ---
case "${1:-}" in
  backup)  shift; cmd_backup "$@" ;;
  restore) shift; cmd_restore "$@" ;;
  verify)  shift; cmd_verify "$@" ;;
  -h|--help|"") usage ;;
  *) usage; die "unknown subcommand: $1" ;;
esac
