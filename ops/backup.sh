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
API_CONTAINER="${API_CONTAINER:-biopipe-api-1}"
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
  API_CONTAINER     api container to talk to (default: biopipe-api-1)
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

  if ! docker exec -i "$API_CONTAINER" python - <<'PY' >>"$outfile" 2>/dev/null
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

counts_to_json() {
  local file="$1"
  awk -F'\t' '
    BEGIN { printf "{" ; sep = "" }
    NF == 2 { printf "%s\"%s\": %s", sep, $1, $2; sep = ", " }
    END { printf "}\n" }
  ' "$file"
}

# Every collection, no allowlist: a collection added later is captured
# without anyone remembering to update this script.
collection_counts() {
  docker exec -i "$MONGO_CONTAINER" mongosh "$MONGO_DB" --quiet --eval '
    db.getCollectionNames().sort().forEach(n => {
      print(n + "\t" + db.getCollection(n).countDocuments({}));
    });
  '
}

write_backup_manifest() {
  local outfile="$1" countsfile="$2" manifestfile="$3"
  local version git_sha blobs total_size
  version="$(cat "$REPO_ROOT/VERSION" 2>/dev/null || echo unknown)"
  git_sha="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
  blobs="$(manifest_row_count "$manifestfile")"
  total_size="$(tail -n +2 "$manifestfile" | awk -F'\t' '{s += $2} END {print s + 0}')"

  cat >"$outfile" <<EOF
{
  "version": "$version",
  "git_sha": "$git_sha",
  "created_utc": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "bioinfo_home": "${BIOINFO_HOME:-unknown}",
  "database": "$MONGO_DB",
  "blob_count": $blobs,
  "blob_total_size": $total_size,
  "collection_counts": $(counts_to_json "$countsfile")
}
EOF
}

version_matches() {
  local backup_version="$1" current_version="$2"
  [ "$backup_version" != "unknown" ] && [ "$backup_version" = "$current_version" ]
}

# Written into every backup, not just the repo: the machine that needs this
# may not have a checkout.
write_restore_doc() {
  cat >"$1" <<'EOF'
# Restoring this backup

    make restore BACKUP=<this directory>

## What this recovers

Everything in the Mongo database: projects, objects and their detected
formats and roles, provenance, run history, and timings.

## What this does NOT recover

**Files in `/data`.** This backup enumerates them in `data-manifest.tsv`
but does not contain them. After restoring, run

    make backup-verify BACKUP=<this directory>

to list which enumerated files are missing from the current `/data`. Many
references and assemblies can be re-downloaded from public sources.

**Provider keys (API keys).** See `providers.txt` for which providers were
configured and the last four characters of each key. Re-enter them under
Settings → AI, where they will show red until you do. The encryption key
is deliberately not in this backup, so nothing here decrypts anything.

## Version contract

Restore into the version you backed up from. `manifest.json` records the
version this was taken at. A mismatch warns and requires `--force`,
attempts **no migration**, and may fail on read if the schema changed.

## Restore is not "reset to exactly this backup"

Collections are dropped and reloaded one by one, so a collection created
*after* this backup was taken survives the restore.
EOF
}

cmd_backup() {
  local stamp dest
  stamp="$(backup_stamp)"
  dest="$BACKUP_DIR/$stamp"

  docker exec -i "$MONGO_CONTAINER" true 2>/dev/null \
    || die "cannot reach Mongo container '$MONGO_CONTAINER'. Is the stack up?"

  mkdir -p "$dest/dump"
  log "Backing up to $dest"

  log "  mongodump…"
  docker exec -i "$MONGO_CONTAINER" mongodump --db "$MONGO_DB" --archive --quiet \
    >"$dest/dump/$MONGO_DB.archive"

  log "  enumerating /data…"
  write_data_manifest "$dest/data-manifest.tsv"

  log "  provider summary…"
  write_provider_summary "$dest/providers.txt"

  log "  manifest…"
  collection_counts >"$dest/.counts.tsv"
  write_backup_manifest "$dest/manifest.json" "$dest/.counts.tsv" "$dest/data-manifest.tsv"
  rm -f "$dest/.counts.tsv"

  write_restore_doc "$dest/RESTORE.md"

  log ""
  log "Backup complete: $dest"
  log "  blobs enumerated: $(manifest_row_count "$dest/data-manifest.tsv")"
  log "  backup size:      $(du -sh "$dest" | cut -f1)"
  log "  backups on disk:  $(find "$BACKUP_DIR" -maxdepth 1 -mindepth 1 -type d | wc -l | tr -d ' ')"
  log ""
  log "This backup does NOT contain /data blobs or provider keys."
  log "A backup on the same disk survives mistakes, not drive failure --"
  log "set BACKUP_DIR= to external storage for that."
}

# A scalar out of manifest.json without a jq dependency. The file is written
# by write_backup_manifest, one field per line, so this is safe here in a way
# it would not be for arbitrary JSON.
json_field() {
  local file="$1" key="$2"
  sed -n "s/.*\"$key\"[[:space:]]*:[[:space:]]*\"\{0,1\}\([^\",]*\)\"\{0,1\}.*/\1/p" "$file" | head -1
}

preflight_backup_dir() {
  local dir="$1"
  [ -d "$dir" ] || die "no such backup directory: $dir"
  [ -d "$dir/dump" ] || die "$dir is missing dump/"
  for f in manifest.json data-manifest.tsv providers.txt RESTORE.md; do
    [ -f "$dir/$f" ] || die "$dir is missing $f"
  done
}

# Rows whose file is absent from /data. Silent when everything is present, so
# it composes: no output means nothing missing.
check_manifest_against_data() {
  local manifest="$1" data_root="$2"
  tail -n +2 "$manifest" | while IFS=$'\t' read -r _id _size path _sha _state; do
    [ -n "$path" ] || continue
    case "$path" in
      /*) [ -e "$path" ] || printf '%s\n' "$path" ;;
      *)  [ -e "$data_root/$path" ] || printf '%s\n' "$path" ;;
    esac
  done
}

cmd_restore() {
  local dir="" force=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --force) force=1; shift ;;
      *) dir="$1"; shift ;;
    esac
  done
  [ -n "$dir" ] || die "usage: ops/backup.sh restore <dir> [--force]"

  preflight_backup_dir "$dir"
  docker exec -i "$MONGO_CONTAINER" true 2>/dev/null \
    || die "cannot reach Mongo container '$MONGO_CONTAINER'. Is the stack up?"

  local backup_version current_version
  backup_version="$(json_field "$dir/manifest.json" version)"
  current_version="$(cat "$REPO_ROOT/VERSION" 2>/dev/null || echo unknown)"

  if ! version_matches "$backup_version" "$current_version"; then
    log "Version mismatch:"
    log "  backup was taken at: $backup_version"
    log "  this checkout is:    $current_version"
    log ""
    log "Restore attempts no schema migration. Restoring across versions may"
    log "fail on read if the schema changed. Re-run with --force to proceed."
    [ "$force" -eq 1 ] || exit 1
    log "Proceeding because --force was given."
  fi

  if [ "$force" -eq 0 ]; then
    [ -t 0 ] || die "restore overwrites '$MONGO_DB' and stdin is not a terminal. Pass --force to proceed unattended."
    log "This overwrites the '$MONGO_DB' database. Type the database name to confirm:"
    local answer; read -r answer
    [ "$answer" = "$MONGO_DB" ] || die "confirmation did not match; nothing was changed"
  fi

  # Braced deliberately: under a UTF-8 LC_CTYPE bash reads the following "…"
  # as part of the identifier, so an unbraced "$dir…" expands ${dir…} and
  # dies on `set -u`. Same for $data_root in cmd_verify.
  log "Restoring from ${dir}…"
  docker exec -i "$MONGO_CONTAINER" mongorestore --archive --drop --quiet \
    <"$dir/dump/$MONGO_DB.archive"

  log "Verifying document counts…"
  local expected actual
  expected="$(json_field "$dir/manifest.json" collection_counts)"
  collection_counts >"/tmp/bioflow-restore-counts.$$"
  actual="$(counts_to_json "/tmp/bioflow-restore-counts.$$")"
  rm -f "/tmp/bioflow-restore-counts.$$"

  local manifest_counts
  manifest_counts="$(sed -n 's/.*"collection_counts": \(.*\)/\1/p' "$dir/manifest.json" | tr -d '\n')"
  if [ "$actual" != "$manifest_counts" ]; then
    log ""
    log "Document counts do not match the backup manifest."
    log "  expected: $manifest_counts"
    log "  actual:   $actual"
    die "restore is incomplete"
  fi
  log "  counts match."

  log ""
  cat "$dir/providers.txt"
  log ""
  log "Blobs enumerated in this backup: $(manifest_row_count "$dir/data-manifest.tsv")"
  log "Restore does not check /data. To see what is missing:"
  log "  make backup-verify BACKUP=$dir"
}

cmd_verify() {
  local dir="" do_hash=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --hash) do_hash=1; shift ;;
      *) dir="$1"; shift ;;
    esac
  done
  [ -n "$dir" ] || die "usage: ops/backup.sh verify <dir> [--hash]"
  preflight_backup_dir "$dir"

  local data_root="${BIOINFO_HOME:-/data}"
  [ -d "$data_root" ] || die "no such data directory: $data_root (set BIOINFO_HOME)"

  log "Checking $(manifest_row_count "$dir/data-manifest.tsv") enumerated blobs against ${data_root}…"

  local missing
  missing="$(check_manifest_against_data "$dir/data-manifest.tsv" "$data_root")"

  if [ -z "$missing" ]; then
    log "All enumerated blobs are present."
  else
    log ""
    log "Missing from $data_root:"
    printf '%s\n' "$missing" | sed 's/^/  /'
    log ""
    log "$(printf '%s\n' "$missing" | grep -c .) file(s) missing."
  fi

  if [ "$do_hash" -eq 1 ]; then
    log ""
    log "Hash check not implemented yet -- see #411 follow-up."
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
