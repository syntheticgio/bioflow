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

# Colons are stripped: valid in ISO-8601, not a valid filename everywhere.
backup_stamp() {
  date -u +"%Y-%m-%dT%H%M%SZ"
}

# One row per blob, written so it stays readable when nothing else works: no
# Mongo, no Docker, no BioFlow. After a disk failure this is the file that
# says what was lost.
#
# `path` is rel_path for a managed blob and external_path for an external one;
# they are mutually exclusive by the model's own uniqueness constraint.
write_data_manifest() {
  local outfile="$1"
  printf 'blob_id\tsize\tpath\tcontent_sha256\tstate\n' >"$outfile"
  docker exec -i "$MONGO_CONTAINER" mongosh "$MONGO_DB" --quiet --eval '
    db.blobs.find({}, {
      _id: 1, size: 1, rel_path: 1, external_path: 1,
      content_sha256: 1, state: 1
    }).forEach(b => {
      print([
        b._id,
        b.size ?? 0,
        b.rel_path ?? b.external_path ?? "",
        b.content_sha256 ?? "",
        b.state ?? ""
      ].join("\t"));
    });
  ' >>"$outfile"
}

manifest_row_count() {
  local file="$1"
  # tail -n +2 drops the header. wc -l on an empty remainder is 0.
  tail -n +2 "$file" | grep -c . || true
}

# The last four characters, enough for a user to confirm which key they are
# pasting back without the value being recoverable from the backup.
key_digest() {
  local key="${1:-}"
  if [ -z "$key" ]; then
    printf '(no key)\n'
  else
    printf '…%s\n' "${key: -4}"
  fi
}

# Which providers were configured, so restore can say what needs re-entering.
# Runs through the api container because decrypting needs Fernet and the key
# file; the key itself never reaches this script.
#
# Best-effort: a stopped api container costs the summary, not the backup.
write_provider_summary() {
  local outfile="$1"
  {
    printf 'Providers configured at backup time.\n'
    printf 'Keys are NOT included in this backup and must be re-entered after restore.\n\n'
  } >"$outfile"

  if ! docker compose exec -T api python - <<'PY' >>"$outfile" 2>/dev/null
import asyncio
from collections import defaultdict

from app.db.client import close_mongo, connect_to_mongo
from app.models.ai import AiProvider, AiRouting
from app.services.ai.crypto import decrypt


async def main() -> None:
    await connect_to_mongo()
    try:
        providers = await AiProvider.find_all().to_list()
        if not providers:
            print("  (none configured)")
            return

        routing = await AiRouting.load()
        slots_by_provider: dict[str, list[str]] = defaultdict(list)
        for slot, provider_id in routing.slots.items():
            slots_by_provider[provider_id].append(slot)
        if routing.default:
            slots_by_provider[routing.default].append("default")

        for p in providers:
            plaintext = decrypt(p.api_key_enc) if p.api_key_enc else None
            digest = f"…{plaintext[-4:]}" if plaintext else "(no key)"
            slots = ", ".join(sorted(slots_by_provider.get(str(p.id), []))) or "unassigned"
            print(f"  {p.name:<12} {digest:<10} {p.model or '-':<24} {slots}")
    finally:
        await close_mongo()


asyncio.run(main())
PY
  then
    printf '  (could not reach the api container; summary unavailable)\n' >>"$outfile"
  fi
}

# --- dispatch ---
case "${1:-}" in
  backup)  shift; cmd_backup "$@" ;;
  restore) shift; cmd_restore "$@" ;;
  verify)  shift; cmd_verify "$@" ;;
  -h|--help|"") usage ;;
  *) usage; die "unknown subcommand: $1" ;;
esac
