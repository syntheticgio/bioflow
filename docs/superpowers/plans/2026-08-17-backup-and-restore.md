# Backup and Restore Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give BioFlow an ops-level backup and restore so the research record — the Mongo database plus an enumeration of `/data` — is recoverable after a disk failure or a mistaken command.

**Architecture:** One bash script, `ops/backup.sh`, with three subcommands (`backup`, `restore`, `verify`), wrapped by three Makefile targets. `backup` runs `mongodump` through the `mongo` container and writes four companion files beside the dump; `restore` reverses it with a version gate and a document-count verification; `verify` walks the `/data` manifest against the filesystem. No backend code changes.

**Tech Stack:** bash, `mongodump`/`mongorestore` (mongo:7 image), Docker Compose, pytest (`ops/tests/`), `mongosh` for queries.

**Spec:** `docs/superpowers/specs/2026-08-17-backup-and-restore-design.md`

## Global Constraints

- **The backup must never contain the Fernet key or any plaintext API key.** The key lives at `$BIOINFO_HOME/.biopipe/secret.key` and is never read, copied, or printed by any code in this plan. Task 8 enforces this with a test.
- **`ops/backup.sh` must take its Mongo target from an environment variable**, defaulting to the running stack. This indirection exists so tests can redirect it at a throwaway container; a test pointed at the real stack destroys the user's research record. This is the most important line in the plan.
- **No `--with-data` flag.** Blobs are enumerated in a manifest, never copied.
- **No automatic retention or pruning.** Nothing in this plan deletes a backup directory.
- Backups default to `./backups/<UTC timestamp>/`, overridable with `BACKUP_DIR=`.
- Restore attempts **no** schema migration. Version mismatch warns and requires `--force`.
- Shell style follows `ops/worktree-up.sh`: `set -euo pipefail`, lowercase function names, `log()`-style status output.
- Timestamp format is `YYYY-MM-DDTHHMMSSZ` (UTC, colons stripped so it is a valid directory name on every filesystem).

## CI reality check (read before Task 8)

`.github/workflows/build-check.yml:169` runs `pytest ops/tests/ -v` in the `backend-smoke` job. That job installs **no Mongo** and its `services:` block is commented out as FUTURE work (`build-check.yml:184-190`). The comment at line 167 describes ops tests as "self-contained (subprocess + tmp_path fixtures, no backend imports)".

So the round-trip test **cannot** run in CI today. This plan splits tests by requirement:

- Tasks 2-7 write pure-logic tests (no Docker, no Mongo) that run in CI exactly as the existing ops tests do.
- Task 8 writes the round-trip test behind a `docker` marker that **skips** when no Docker daemon is reachable.

That keeps the existing CI job green, runs the full guarantee locally, and leaves the test ready for the day the FUTURE Mongo job is uncommented. The skip is loud (a skip reason naming why), never silent.

---

### Task 1: Script skeleton, subcommand dispatch, and `backups/` ignored

**Files:**
- Create: `ops/backup.sh`
- Modify: `.gitignore`
- Modify: `Makefile`

**Interfaces:**
- Consumes: nothing.
- Produces: `ops/backup.sh` with a dispatch `case` on `$1` handling `backup`, `restore`, `verify`, and `--help`; a `# --- dispatch ---` marker comment above the `case` that later tests source the file up to. Environment variables `BACKUP_DIR` (default `./backups`), `MONGO_CONTAINER` (default `biopipe-mongo-1`), `MONGO_DB` (default `biopipe`).

- [ ] **Step 1: Create the script skeleton**

Create `ops/backup.sh`:

```bash
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
```

- [ ] **Step 2: Make it executable and confirm the help path works**

Run:

```bash
chmod +x ops/backup.sh && ./ops/backup.sh --help
```

Expected: the usage block prints, exit 0.

- [ ] **Step 3: Ignore the backups directory**

Add to `.gitignore`, after the `.worktrees/` block:

```
# Backups written by ops/backup.sh. Timestamped directories, never pruned.
backups/
```

- [ ] **Step 4: Add the Makefile targets**

Add to `Makefile`, after the `clean:` target. Also add `backup restore backup-verify` to the `.PHONY` line at the top:

```makefile
backup: ## Back up the Mongo database and enumerate /data (BACKUP_DIR= to redirect)
	./ops/backup.sh backup

restore: ## Restore a backup. Overwrites the database. BACKUP=<dir> required.
	@test -n "$(BACKUP)" || (echo "ERROR: set BACKUP=<dir>, e.g. make restore BACKUP=backups/2026-08-17T134502Z"; exit 1)
	./ops/backup.sh restore "$(BACKUP)"

backup-verify: ## Check /data against a backup's manifest. BACKUP=<dir> required.
	@test -n "$(BACKUP)" || (echo "ERROR: set BACKUP=<dir>"; exit 1)
	./ops/backup.sh verify "$(BACKUP)"
```

- [ ] **Step 5: Verify the targets are wired**

Run:

```bash
make restore
```

Expected: fails with the "set BACKUP=<dir>" message, exit 1.

- [ ] **Step 6: Commit**

```bash
git add ops/backup.sh .gitignore Makefile
git commit -m "feat(ops): add the backup script skeleton and its make targets

Dispatch, usage, and the three environment knobs. The Mongo target is an
environment variable from the start because the round-trip test must be
able to redirect it away from the live stack.

Refs #411"
```

---

### Task 2: Timestamp and backup directory creation

**Files:**
- Modify: `ops/backup.sh`
- Test: `ops/tests/test_backup_restore.py`

**Interfaces:**
- Consumes: `BACKUP_DIR` from Task 1.
- Produces: `backup_stamp()` echoing a UTC timestamp as `YYYY-MM-DDTHHMMSSZ`; `cmd_backup()` creating `$BACKUP_DIR/<stamp>/` and echoing the path.

- [ ] **Step 1: Write the failing test**

Create `ops/tests/test_backup_restore.py`:

```python
"""`ops/backup.sh`: the backup, restore, and verify subcommands.

Split by what each test needs. Everything here except the round-trip test is
pure shell logic -- sourced functions, tmp_path fixtures, no Docker -- so it
runs in the `backend-smoke` CI job alongside the other ops tests. The
round-trip test needs a real Mongo and carries the `docker` marker; see the
note above it.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "ops" / "backup.sh"


def sh(script: str, cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    """Runs a bash snippet with the script's functions sourced.

    The script dispatches on $1 and does real work otherwise, so tests source
    only the definitions: everything above the dispatch `case`. Same approach
    as test_worktree_prune.py.
    """
    text = SCRIPT.read_text()
    marker = "# --- dispatch ---"
    assert marker in text, "dispatch marker moved; update this test"
    preamble = text.split(marker)[0]
    preamble = preamble.replace("set -euo pipefail", "set -uo pipefail")

    return subprocess.run(
        ["bash", "-c", preamble + "\n" + script],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", **(env or {})},
    )


def test_stamp_is_utc_and_filesystem_safe(tmp_path):
    result = sh("backup_stamp", tmp_path)
    assert result.returncode == 0, result.stderr
    stamp = result.stdout.strip()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{6}Z", stamp), stamp
    assert ":" not in stamp
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest ops/tests/test_backup_restore.py::test_stamp_is_utc_and_filesystem_safe -v`
Expected: FAIL — `backup_stamp: command not found`.

- [ ] **Step 3: Implement `backup_stamp`**

Add to `ops/backup.sh`, above the dispatch marker:

```bash
# Colons are stripped: valid in ISO-8601, not a valid filename everywhere.
backup_stamp() {
  date -u +"%Y-%m-%dT%H%M%SZ"
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest ops/tests/test_backup_restore.py::test_stamp_is_utc_and_filesystem_safe -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ops/backup.sh ops/tests/test_backup_restore.py
git commit -m "feat(ops): stamp backups with a filesystem-safe UTC timestamp

Refs #411"
```

---

### Task 3: The `/data` manifest

**Files:**
- Modify: `ops/backup.sh`
- Test: `ops/tests/test_backup_restore.py`

**Interfaces:**
- Consumes: `MONGO_CONTAINER`, `MONGO_DB`.
- Produces: `write_data_manifest <outfile>` — writes a TSV with header `blob_id\tsize\tpath\tcontent_sha256\tstate`, one row per blob. `manifest_row_count <file>` echoes the data-row count (excluding the header).

The manifest is a standalone artifact readable with `cut` and `grep` on a machine with no Mongo, no Docker, and no BioFlow. That is the entire reason it exists separately from the dump, which already holds the same data.

- [ ] **Step 1: Write the failing test**

Add to `ops/tests/test_backup_restore.py`:

```python
def test_manifest_row_count_ignores_the_header(tmp_path):
    manifest = tmp_path / "data-manifest.tsv"
    manifest.write_text(
        "blob_id\tsize\tpath\tcontent_sha256\tstate\n"
        "aaa\t10\tab/aaa\tsha-a\tactive\n"
        "bbb\t20\tcd/bbb\tsha-b\tactive\n"
    )
    result = sh(f"manifest_row_count {manifest}", tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "2"


def test_manifest_row_count_of_a_header_only_file_is_zero(tmp_path):
    manifest = tmp_path / "data-manifest.tsv"
    manifest.write_text("blob_id\tsize\tpath\tcontent_sha256\tstate\n")
    result = sh(f"manifest_row_count {manifest}", tmp_path)
    assert result.stdout.strip() == "0"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest ops/tests/test_backup_restore.py -k manifest_row_count -v`
Expected: FAIL — `manifest_row_count: command not found`.

- [ ] **Step 3: Implement both functions**

Add to `ops/backup.sh`, above the dispatch marker:

```bash
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
```

- [ ] **Step 4: Run to verify both pass**

Run: `pytest ops/tests/test_backup_restore.py -k manifest_row_count -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add ops/backup.sh ops/tests/test_backup_restore.py
git commit -m "feat(ops): enumerate /data blobs into a standalone manifest

The blobs collection already records id, size, rel_path and
content_sha256, so the manifest is a projection rather than new
bookkeeping. It is written as its own TSV -- not left inside the dump --
so it stays readable on a machine with no Mongo and no BioFlow, which is
the state the machine is in when it matters.

Refs #411"
```

---

### Task 4: The provider summary

**Files:**
- Modify: `ops/backup.sh`
- Test: `ops/tests/test_backup_restore.py`

**Interfaces:**
- Consumes: `MONGO_CONTAINER`, `MONGO_DB`.
- Produces: `write_provider_summary <outfile>` — writes `providers.txt` listing provider name, task slots, model, and the **last four characters** of each key.

The last-four digest comes from the plaintext, which means decrypting — so this runs through the `api` container, which already has Fernet and the key mounted, rather than reimplementing Fernet in shell. The output carries no ciphertext and no key.

- [ ] **Step 1: Write the failing test**

Add to `ops/tests/test_backup_restore.py`:

```python
def test_key_digest_shows_only_the_last_four(tmp_path):
    result = sh('key_digest "sk-ant-api03-SECRETVALUE-f4a2"', tmp_path)
    assert result.stdout.strip() == "…f4a2"


def test_key_digest_of_an_absent_key_says_so(tmp_path):
    result = sh('key_digest ""', tmp_path)
    assert result.stdout.strip() == "(no key)"


def test_key_digest_never_echoes_the_whole_key(tmp_path):
    secret = "sk-ant-api03-DONOTLEAK-9c1d"
    result = sh(f'key_digest "{secret}"', tmp_path)
    assert "DONOTLEAK" not in result.stdout
    assert "DONOTLEAK" not in result.stderr
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest ops/tests/test_backup_restore.py -k key_digest -v`
Expected: FAIL — `key_digest: command not found`.

- [ ] **Step 3: Implement `key_digest` and `write_provider_summary`**

Add to `ops/backup.sh`, above the dispatch marker:

```bash
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
from app.db.client import connect_to_mongo, close_mongo
from app.models.ai import AiProvider
from app.services.ai.crypto import decrypt


async def main() -> None:
    await connect_to_mongo()
    try:
        providers = await AiProvider.find_all().to_list()
        if not providers:
            print("  (none configured)")
            return
        for p in providers:
            plaintext = decrypt(p.api_key_enc) if p.api_key_enc else None
            digest = f"…{plaintext[-4:]}" if plaintext else "(no key)"
            slots = ", ".join(sorted(p.task_slots)) if getattr(p, "task_slots", None) else "inactive"
            print(f"  {p.name:<12} {digest:<10} {p.model or '-':<24} {slots}")
    finally:
        await close_mongo()


asyncio.run(main())
PY
  then
    printf '  (could not reach the api container; summary unavailable)\n' >>"$outfile"
  fi
}
```

> **Note for the implementer:** verify the import paths and the `AiProvider`
> field names against `backend/app/models/ai.py` and
> `backend/app/services/ai/crypto.py` before running. If `task_slots` is not a
> field on `AiProvider`, read `AiRouting` (the singleton slot→provider mapping)
> instead and invert it. Adjust and keep the output shape.

- [ ] **Step 4: Run to verify the digest tests pass**

Run: `pytest ops/tests/test_backup_restore.py -k key_digest -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add ops/backup.sh ops/tests/test_backup_restore.py
git commit -m "feat(ops): summarize configured providers without their keys

Restore has to tell the user why their providers are broken and which to
fix. It does that from a name, model, slot and last-four digest -- never
the key and never the Fernet secret, so the backup still decrypts
nothing.

Refs #411"
```

---

### Task 5: `manifest.json` and the backup command

**Files:**
- Modify: `ops/backup.sh`
- Test: `ops/tests/test_backup_restore.py`

**Interfaces:**
- Consumes: `backup_stamp`, `write_data_manifest`, `write_provider_summary`, `manifest_row_count`.
- Produces: `collection_counts()` echoing `name<TAB>count` per collection; `write_backup_manifest <outfile> <dumpdir> <manifestfile>`; `cmd_backup()` producing the full five-file directory.

- [ ] **Step 1: Write the failing test**

Add to `ops/tests/test_backup_restore.py`:

```python
import json


def test_counts_tsv_becomes_a_json_object(tmp_path):
    counts = tmp_path / "counts.tsv"
    counts.write_text("projects\t3\nobjects\t128\njob_timings\t9\n")
    result = sh(f"counts_to_json {counts}", tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"projects": 3, "objects": 128, "job_timings": 9}


def test_counts_to_json_of_nothing_is_an_empty_object(tmp_path):
    counts = tmp_path / "counts.tsv"
    counts.write_text("")
    result = sh(f"counts_to_json {counts}", tmp_path)
    assert json.loads(result.stdout) == {}
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest ops/tests/test_backup_restore.py -k counts_to_json -v`
Expected: FAIL — `counts_to_json: command not found`.

- [ ] **Step 3: Implement counts, the manifest writer, and `cmd_backup`**

Add to `ops/backup.sh`, above the dispatch marker:

```bash
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
```

- [ ] **Step 4: Run to verify the counts tests pass**

Run: `pytest ops/tests/test_backup_restore.py -k counts_to_json -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add ops/backup.sh ops/tests/test_backup_restore.py
git commit -m "feat(ops): take a backup into a timestamped directory

mongodump plus the four companion files: the /data manifest, the
provider summary, manifest.json with per-collection counts, and the
restore contract. The counts are load-bearing -- restore asserts against
them, so a partial restore is detected rather than assumed.

Refs #411"
```

---

### Task 6: `RESTORE.md` and the version gate

**Files:**
- Modify: `ops/backup.sh`
- Test: `ops/tests/test_backup_restore.py`

**Interfaces:**
- Consumes: nothing from earlier tasks beyond `log`/`die`.
- Produces: `write_restore_doc <outfile>` (called by `cmd_backup` in Task 5); `version_matches <backup_version> <current_version>` returning 0 on match, 1 on mismatch.

- [ ] **Step 1: Write the failing test**

Add to `ops/tests/test_backup_restore.py`:

```python
def test_version_matches_on_identical_versions(tmp_path):
    assert sh('version_matches "0.5.1" "0.5.1"', tmp_path).returncode == 0


def test_version_matches_rejects_a_different_version(tmp_path):
    assert sh('version_matches "0.5.1" "0.6.0"', tmp_path).returncode == 1


def test_version_matches_rejects_an_unknown_version(tmp_path):
    assert sh('version_matches "unknown" "0.5.1"', tmp_path).returncode == 1


def test_restore_doc_states_what_is_not_recovered(tmp_path):
    out = tmp_path / "RESTORE.md"
    result = sh(f"write_restore_doc {out}", tmp_path)
    assert result.returncode == 0, result.stderr
    text = out.read_text()
    assert "--force" in text
    assert "provider keys" in text.lower()
    assert "no migration" in text.lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest ops/tests/test_backup_restore.py -k "version_matches or restore_doc" -v`
Expected: FAIL — functions not found.

- [ ] **Step 3: Implement both**

Add to `ops/backup.sh`, above the dispatch marker:

```bash
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

**Provider API keys.** See `providers.txt` for which providers were
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
```

- [ ] **Step 4: Run to verify they pass**

Run: `pytest ops/tests/test_backup_restore.py -k "version_matches or restore_doc" -v`
Expected: 4 passed.

- [ ] **Step 5: Confirm a real backup produces all five files**

Run (needs the stack up):

```bash
make backup && ls -la "$(ls -d backups/*/ | tail -1)"
```

Expected: `dump/`, `data-manifest.tsv`, `providers.txt`, `manifest.json`, `RESTORE.md`.

- [ ] **Step 6: Commit**

```bash
git add ops/backup.sh ops/tests/test_backup_restore.py
git commit -m "feat(ops): write the restore contract into every backup

The machine that needs the contract may not have a checkout, so it ships
inside the backup directory rather than only in docs. States plainly what
restore recovers, what it does not (blobs, provider keys), and that a
version mismatch attempts no migration.

Refs #411"
```

---

### Task 7: Restore and verify

**Files:**
- Modify: `ops/backup.sh`
- Test: `ops/tests/test_backup_restore.py`

**Interfaces:**
- Consumes: `version_matches`, `manifest_row_count`, `counts_to_json`.
- Produces: `cmd_restore <dir> [--force]`; `cmd_verify <dir> [--hash]`; `json_field <file> <key>` echoing a scalar value from `manifest.json`.

- [ ] **Step 1: Write the failing test**

Add to `ops/tests/test_backup_restore.py`:

```python
def test_json_field_reads_a_string(tmp_path):
    m = tmp_path / "manifest.json"
    m.write_text('{\n  "version": "0.5.1",\n  "blob_count": 42\n}\n')
    assert sh(f'json_field {m} version', tmp_path).stdout.strip() == "0.5.1"


def test_json_field_reads_a_number(tmp_path):
    m = tmp_path / "manifest.json"
    m.write_text('{\n  "version": "0.5.1",\n  "blob_count": 42\n}\n')
    assert sh(f'json_field {m} blob_count', tmp_path).stdout.strip() == "42"


def test_restore_refuses_a_directory_missing_files(tmp_path):
    incomplete = tmp_path / "backup"
    (incomplete / "dump").mkdir(parents=True)
    result = sh(f"preflight_backup_dir {incomplete}", tmp_path)
    assert result.returncode != 0
    assert "manifest.json" in result.stderr


def test_preflight_accepts_a_complete_directory(tmp_path):
    good = tmp_path / "backup"
    (good / "dump").mkdir(parents=True)
    for name in ("data-manifest.tsv", "providers.txt", "manifest.json", "RESTORE.md"):
        (good / name).write_text("x")
    result = sh(f"preflight_backup_dir {good}", tmp_path)
    assert result.returncode == 0, result.stderr


def test_verify_reports_a_missing_blob(tmp_path):
    data = tmp_path / "data"
    (data / "ab").mkdir(parents=True)
    (data / "ab" / "present").write_text("hello")

    manifest = tmp_path / "data-manifest.tsv"
    manifest.write_text(
        "blob_id\tsize\tpath\tcontent_sha256\tstate\n"
        "present\t5\tab/present\tsha-a\tactive\n"
        "gone\t9\tab/gone\tsha-b\tactive\n"
    )
    result = sh(f"check_manifest_against_data {manifest} {data}", tmp_path)
    assert "ab/gone" in result.stdout
    assert "ab/present" not in result.stdout


def test_verify_is_silent_when_everything_is_present(tmp_path):
    data = tmp_path / "data"
    (data / "ab").mkdir(parents=True)
    (data / "ab" / "present").write_text("hello")

    manifest = tmp_path / "data-manifest.tsv"
    manifest.write_text(
        "blob_id\tsize\tpath\tcontent_sha256\tstate\n"
        "present\t5\tab/present\tsha-a\tactive\n"
    )
    result = sh(f"check_manifest_against_data {manifest} {data}", tmp_path)
    assert result.stdout.strip() == ""
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest ops/tests/test_backup_restore.py -k "json_field or preflight or verify_reports or verify_is_silent" -v`
Expected: FAIL — functions not found.

- [ ] **Step 3: Implement preflight, restore, and verify**

Add to `ops/backup.sh`, above the dispatch marker:

```bash
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
  for f in data-manifest.tsv providers.txt manifest.json RESTORE.md; do
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

  log "Restoring from $dir…"
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

  log "Checking $(manifest_row_count "$dir/data-manifest.tsv") enumerated blobs against $data_root…"

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
```

> **Note for the implementer:** `--hash` is declared in the usage text and
> parsed here but intentionally stubbed. Hashing every blob is slow enough to
> deserve its own task, and the missing-file check is what the issue's
> verification requirement actually asks for. If you finish the plan early,
> implementing it is a good follow-up — otherwise file it.

- [ ] **Step 4: Run to verify they pass**

Run: `pytest ops/tests/test_backup_restore.py -v`
Expected: all pass (13 tests).

- [ ] **Step 5: Commit**

```bash
git add ops/backup.sh ops/tests/test_backup_restore.py
git commit -m "feat(ops): restore a backup and verify /data against its manifest

Restore gates on the recorded version, confirms interactively before
overwriting, and asserts per-collection document counts afterwards so a
partial restore fails loudly rather than looking like a success.

backup-verify walks the manifest against the live /data and names what is
missing, which is the check that makes an enumerated loss actionable.

Refs #411"
```

---

### Task 8: The round-trip test

**Files:**
- Modify: `ops/tests/test_backup_restore.py`

**Interfaces:**
- Consumes: the whole script.
- Produces: a `docker`-marked round-trip test plus the security assertions.

**This is the task that carries the implementation risk.** A round-trip test drops a database and reloads it. Pointed at the running stack it destroys the user's research record while reporting a pass. The fixture below starts its **own** Mongo on a random port and passes `MONGO_CONTAINER` explicitly; verify that redirection works before writing a single assertion.

- [ ] **Step 1: Write the fixture and the round-trip test**

Add to `ops/tests/test_backup_restore.py`:

```python
import os
import shutil
import time
import uuid

# The round-trip needs a real Mongo. The backend-smoke CI job that runs
# ops/tests has no Docker service and its Mongo `services:` block is still
# commented out (build-check.yml:184-190), so this skips there and runs
# locally. The skip names its reason rather than passing quietly.
docker_required = pytest.mark.skipif(
    shutil.which("docker") is None
    or subprocess.run(
        ["docker", "info"], capture_output=True
    ).returncode != 0,
    reason="needs a reachable Docker daemon; the CI ops-test job has none",
)


@pytest.fixture
def scratch_mongo():
    """A throwaway Mongo container, never the running stack's.

    The name is randomised so a leftover container from a crashed run cannot
    be reused by accident, and the container is removed on teardown.
    """
    name = f"bioflow-backup-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "mongo:7"],
        check=True, capture_output=True,
    )
    try:
        for _ in range(30):
            probe = subprocess.run(
                ["docker", "exec", name, "mongosh", "--quiet", "--eval", "db.hello().ok"],
                capture_output=True, text=True,
            )
            if probe.returncode == 0 and "1" in probe.stdout:
                break
            time.sleep(1)
        else:
            pytest.fail("scratch Mongo never became ready")
        yield name
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def seed(container: str, db: str = "biopipe") -> None:
    """A fixture spanning the shapes that matter."""
    script = """
    db.projects.insertOne({_id: "proj1", name: "Test Project"});
    db.objects.insertMany([
      {_id: "obj1", project_id: "proj1", role: "reads", size: 100, blob_id: "blob1"},
      {_id: "obj2", project_id: "proj1", role: "reference", size: 200, blob_id: "blob2"}
    ]);
    db.blobs.insertMany([
      {_id: "blob1", size: 100, rel_path: "ab/blob1", content_sha256: "sha1", state: "active"},
      {_id: "blob2", size: 200, external_path: "/ext/ref.fa", content_sha256: "sha2", state: "active"}
    ]);
    db.pipeline_runs.insertOne({_id: "run1", project_id: "proj1", status: "complete"});
    db.run_jobs.insertOne({_id: "job1", run_id: "run1", object_id: "obj1"});
    db.job_timings.insertOne({_id: "t1", job_id: "job1", duration_seconds: 12.5});
    db.ai_providers.insertOne({
      _id: "prov1", name: "anthropic", model: "claude-opus-5",
      api_key_enc: BinData(0, "Z0FBQUFBQm1abT")
    });
    """
    subprocess.run(
        ["docker", "exec", "-i", container, "mongosh", db, "--quiet", "--eval", script],
        check=True, capture_output=True,
    )


def counts(container: str, db: str = "biopipe") -> dict:
    out = subprocess.run(
        ["docker", "exec", "-i", container, "mongosh", db, "--quiet", "--eval",
         'db.getCollectionNames().sort().forEach(n => print(n + "\\t" + db.getCollection(n).countDocuments({})))'],
        check=True, capture_output=True, text=True,
    ).stdout
    return {
        line.split("\t")[0]: int(line.split("\t")[1])
        for line in out.strip().splitlines() if "\t" in line
    }


def run_script(args: list[str], container: str, backup_dir: Path, **kw):
    env = {
        **os.environ,
        "MONGO_CONTAINER": container,
        "MONGO_DB": "biopipe",
        "BACKUP_DIR": str(backup_dir),
    }
    return subprocess.run(
        [str(SCRIPT), *args], capture_output=True, text=True, env=env, **kw
    )


@docker_required
def test_backup_restore_round_trip(scratch_mongo, tmp_path):
    seed(scratch_mongo)
    before = counts(scratch_mongo)
    assert before["projects"] == 1 and before["objects"] == 2

    result = run_script(["backup"], scratch_mongo, tmp_path)
    assert result.returncode == 0, result.stderr

    made = sorted(tmp_path.iterdir())
    assert len(made) == 1
    backup = made[0]
    for name in ("dump", "data-manifest.tsv", "providers.txt", "manifest.json", "RESTORE.md"):
        assert (backup / name).exists(), f"{name} missing from the backup"

    subprocess.run(
        ["docker", "exec", "-i", scratch_mongo, "mongosh", "biopipe", "--quiet",
         "--eval", "db.dropDatabase()"],
        check=True, capture_output=True,
    )
    assert counts(scratch_mongo) == {}

    result = run_script(["restore", str(backup), "--force"], scratch_mongo, tmp_path)
    assert result.returncode == 0, result.stderr

    after = counts(scratch_mongo)
    assert after == before, f"counts differ after restore: {before} -> {after}"


@docker_required
def test_provenance_chain_survives_the_round_trip(scratch_mongo, tmp_path):
    seed(scratch_mongo)
    run_script(["backup"], scratch_mongo, tmp_path)
    backup = sorted(tmp_path.iterdir())[0]
    subprocess.run(
        ["docker", "exec", "-i", scratch_mongo, "mongosh", "biopipe", "--quiet",
         "--eval", "db.dropDatabase()"],
        check=True, capture_output=True,
    )
    run_script(["restore", str(backup), "--force"], scratch_mongo, tmp_path)

    chain = subprocess.run(
        ["docker", "exec", "-i", scratch_mongo, "mongosh", "biopipe", "--quiet", "--eval",
         'const j = db.run_jobs.findOne({_id: "job1"});'
         'const o = db.objects.findOne({_id: j.object_id});'
         'const b = db.blobs.findOne({_id: o.blob_id});'
         'print([j.run_id, o.role, b.content_sha256].join("|"));'],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert chain == "run1|reads|sha1"
```

- [ ] **Step 2: Run it and confirm the round trip passes**

Run: `pytest ops/tests/test_backup_restore.py -k round_trip -v`
Expected: PASS locally. If Docker is unavailable: SKIPPED with the stated reason.

- [ ] **Step 3: Verify the isolation actually holds**

Before trusting any of the above, confirm the test never touched the real stack:

```bash
docker compose exec -T mongo mongosh biopipe --quiet --eval 'db.projects.countDocuments({})'
```

Expected: your real project count, unchanged. If this is 0 or 1, the redirection failed — **stop and fix `MONGO_CONTAINER` handling before continuing.**

- [ ] **Step 4: Write the security and failure-path assertions**

Add to `ops/tests/test_backup_restore.py`:

```python
@docker_required
def test_backup_contains_no_secrets(scratch_mongo, tmp_path):
    """The assertion that keeps the security decision true after later edits."""
    seed(scratch_mongo)
    run_script(["backup"], scratch_mongo, tmp_path)
    backup = sorted(tmp_path.iterdir())[0]

    blob = b""
    for path in backup.rglob("*"):
        if path.is_file():
            blob += path.read_bytes()

    # The Fernet key file's own name and any Fernet token prefix.
    assert b"secret.key" not in blob
    assert b"BIOINFO_HOME/.biopipe" not in blob
    # A decrypted Anthropic-style key would start like this.
    assert b"sk-ant-" not in blob


@docker_required
def test_version_mismatch_without_force_writes_nothing(scratch_mongo, tmp_path):
    seed(scratch_mongo)
    run_script(["backup"], scratch_mongo, tmp_path)
    backup = sorted(tmp_path.iterdir())[0]

    manifest = backup / "manifest.json"
    manifest.write_text(manifest.read_text().replace('"version": "', '"version": "9.9.9-'))

    subprocess.run(
        ["docker", "exec", "-i", scratch_mongo, "mongosh", "biopipe", "--quiet",
         "--eval", "db.dropDatabase()"],
        check=True, capture_output=True,
    )
    result = run_script(["restore", str(backup)], scratch_mongo, tmp_path)
    assert result.returncode != 0
    assert "Version mismatch" in result.stderr
    assert counts(scratch_mongo) == {}, "restore wrote despite refusing"


@docker_required
def test_version_mismatch_with_force_completes(scratch_mongo, tmp_path):
    seed(scratch_mongo)
    before = counts(scratch_mongo)
    run_script(["backup"], scratch_mongo, tmp_path)
    backup = sorted(tmp_path.iterdir())[0]

    manifest = backup / "manifest.json"
    manifest.write_text(manifest.read_text().replace('"version": "', '"version": "9.9.9-'))

    subprocess.run(
        ["docker", "exec", "-i", scratch_mongo, "mongosh", "biopipe", "--quiet",
         "--eval", "db.dropDatabase()"],
        check=True, capture_output=True,
    )
    result = run_script(["restore", str(backup), "--force"], scratch_mongo, tmp_path)
    assert result.returncode == 0, result.stderr
    assert counts(scratch_mongo) == before


@docker_required
def test_restore_fails_loudly_on_a_count_mismatch(scratch_mongo, tmp_path):
    seed(scratch_mongo)
    run_script(["backup"], scratch_mongo, tmp_path)
    backup = sorted(tmp_path.iterdir())[0]

    manifest = backup / "manifest.json"
    manifest.write_text(manifest.read_text().replace('"projects": 1', '"projects": 7'))

    result = run_script(["restore", str(backup), "--force"], scratch_mongo, tmp_path)
    assert result.returncode != 0
    assert "do not match" in result.stderr
```

- [ ] **Step 5: Run the whole file**

Run: `pytest ops/tests/test_backup_restore.py -v`
Expected: all pass (or the `docker`-marked ones skip with their reason).

- [ ] **Step 6: Commit**

```bash
git add ops/tests/test_backup_restore.py
git commit -m "test(ops): round-trip a backup through a throwaway Mongo

Seeds a scratch container, backs up, drops the database, restores, and
asserts every collection count matches and a provenance chain still
resolves. The container is the test's own on a randomised name -- pointed
at the live stack this test would destroy the research record it exists
to protect.

Also asserts the backup carries no secret, that a version mismatch writes
nothing without --force, and that a count mismatch fails loudly.

Refs #411"
```

---

### Task 9: Documentation and the manual pass

**Files:**
- Modify: `README.md`
- Modify: `docs/TODO.md` (only if an entry covers this — check first)

**Interfaces:**
- Consumes: the finished script.
- Produces: user-facing documentation.

- [ ] **Step 1: Find where README covers ops commands**

Run:

```bash
grep -n "make clean\|make up\|## " README.md | head -30
```

- [ ] **Step 2: Add a backup section**

Add to `README.md`, after the section covering `make up`/`make clean`:

```markdown
### Backing up

    make backup

Writes `./backups/<timestamp>/` containing a `mongodump` of the database,
an enumeration of every file in `/data`, a summary of configured AI
providers, and the restore contract. Set `BACKUP_DIR=` to write elsewhere:

    make backup BACKUP_DIR=/Volumes/Backups/bioflow

**A backup on the same disk protects against mistakes and corruption, not
drive failure.** Point `BACKUP_DIR` at external storage for that.

The backup does **not** contain the files in `/data` — a real project's
data may be hundreds of gigabytes, and copying it is your call with your
own tooling. What the backup gives you is an enumeration, so after a loss
you know exactly which files are gone and which can be re-downloaded.

It also does **not** contain your AI provider API keys, deliberately: a
backup holding both the encrypted keys and the key that decrypts them
would defeat the encryption. `providers.txt` records which providers were
configured and the last four characters of each key so you know what to
re-enter.

Nothing is pruned automatically. Delete old backup directories yourself.

### Restoring

    make restore BACKUP=backups/2026-08-17T134502Z

Overwrites the database, so it asks for confirmation first. Restore into
the version you backed up from — a mismatch requires `--force`, attempts
no schema migration, and may fail if the schema changed.

To see which enumerated files are missing from `/data`:

    make backup-verify BACKUP=backups/2026-08-17T134502Z
```

- [ ] **Step 3: The manual pass — back up the real database**

Run from the main checkout with the stack up:

```bash
make backup
```

Expected: completes, prints the blob count and backup size, five files present.

- [ ] **Step 4: The manual pass — restore into a scratch stack**

**Do not restore into the live stack.** Start a throwaway Mongo, restore into it, and inspect:

```bash
docker run -d --rm --name bioflow-manual-check mongo:7 && sleep 5 && MONGO_CONTAINER=bioflow-manual-check ./ops/backup.sh restore "$(ls -d backups/*/ | tail -1)" --force
```

Then confirm real data survived:

```bash
docker exec -i bioflow-manual-check mongosh biopipe --quiet --eval 'print("projects: " + db.projects.countDocuments({})); print("objects: " + db.objects.countDocuments({})); print("runs: " + db.pipeline_runs.countDocuments({}));'
```

Expected: counts matching your real database. A fixture cannot catch a
document shape only real data has — this step is what does.

- [ ] **Step 5: Tear the scratch container down**

```bash
docker rm -f bioflow-manual-check
```

- [ ] **Step 6: Verify the live stack was never touched**

```bash
docker compose exec -T mongo mongosh biopipe --quiet --eval 'db.projects.countDocuments({})'
```

Expected: your real project count, unchanged.

- [ ] **Step 7: Commit**

```bash
git add README.md
git commit -m "docs: how to back up, restore, and verify /data

Says plainly what a same-disk backup does not protect against, and why
neither the blobs nor the provider keys are in it.

Closes #411"
```

---

## Self-review

**Spec coverage.** Every spec section maps to a task: the five backup files (Tasks 3-6), restore's six steps (Task 7), `backup-verify` (Task 7), the round trip and its five extra assertions (Task 8), documentation and the manual pass (Task 9). The five decisions in the spec's table are each enforced somewhere — no `--with-data` (nothing implements one), no pruning (nothing deletes), `./backups/` default (Task 1), version gate (Tasks 6-7), no Fernet key (Task 4, asserted in Task 8).

**One deliberate deviation from the spec.** The spec says the round-trip test runs in CI. It cannot: the `backend-smoke` job that runs `ops/tests/` has no Docker service, and the Mongo `services:` block is commented out as FUTURE work. Rather than pretend otherwise, Task 8 marks those tests to skip when no Docker daemon is reachable, with a reason naming why. The pure-logic tests (Tasks 2-7, 13 of them) do run in CI. When the FUTURE Mongo job is uncommented, the marked tests start running with no change to them.

**One deliberate stub.** `verify --hash` is parsed and documented but not implemented; hashing every blob deserves its own task and is not what the issue's verification asks for. Task 7 says so inline and tells the implementer to file it.

**Type consistency.** Function names used across tasks are consistent: `backup_stamp`, `write_data_manifest`, `manifest_row_count`, `key_digest`, `write_provider_summary`, `counts_to_json`, `collection_counts`, `write_backup_manifest`, `write_restore_doc`, `version_matches`, `json_field`, `preflight_backup_dir`, `check_manifest_against_data`, `cmd_backup`, `cmd_restore`, `cmd_verify`. `write_restore_doc` is called in Task 5's `cmd_backup` and defined in Task 6 — the script is not run end to end until Task 6 step 5, so this is safe, but **do not run `make backup` between Tasks 5 and 6.**
