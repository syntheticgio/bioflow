# Backup and restore for the research record

**Issue:** [#411](https://github.com/syntheticgio/bioflow/issues/411)
**Date:** 2026-08-17
**Status:** design approved, ready for implementation plan

## Problem

Nothing in this application can take a backup. The Mongo database plus
`/data` *is* the user's research record -- files, detected formats and roles,
provenance, run history, timings, and every pipeline result -- and one disk
failure or one mistaken command takes all of it with nothing offering a way to
have prepared.

Two details found while designing this sharpen the picture:

- Mongo lives at `~/.bioflow/mongo` on a bind mount, not in a Docker volume,
  so `make clean` and `docker compose down -v` do **not** destroy it. The
  issue's framing overstates that particular risk. Disk failure and
  `rm -rf ~/.bioflow` remain fully unprotected.
- `blobs` already records `id` (sha256 of stored bytes), `size`, `rel_path`,
  `external_path`, and `content_sha256`. A `/data` manifest is therefore a
  projection of data the app already holds, not new bookkeeping.

## Scope

Tier 1 of the issue only: an ops-level backup and restore. Tier 2 (in-app
per-project export) is split to a follow-up issue -- it shares no
implementation with this work, and it is a collaboration feature rather than a
recovery one.

No backend change. No new models, endpoints, or UI. The deliverable is one
script, three Makefile targets, one pytest file, a `.gitignore` line, and the
restore contract in writing.

## Decisions

Each of the issue's five open questions, resolved:

| # | Question | Decision |
|---|---|---|
| 1 | Blobs or manifest only? | **Manifest only.** No `--with-data`. |
| 2 | Where do backups land; retention? | **`./backups/<timestamp>/`**, `BACKUP_DIR=` override, no pruning. |
| 3 | Tier 2 scope | **Out of scope**, split to a follow-up issue. |
| 4 | Version-mismatch contract | **Record and warn.** Mismatch requires `--force`; no migration attempted. |
| 5 | Redaction | **Exclude the Fernet key.** Ciphertext only, plus a non-decrypting provider summary. |

### 1. Manifest, not blobs

Copying `/data` is `rsync`'s job. A real project's `/data` may be hundreds of
gigabytes, the copy is the user's call with their own tooling, and a
half-finished copy produces a backup directory that looks complete and is not.

`--with-data` is deliberately **not** implemented, not even as an opt-in. The
manifest is the part BioFlow uniquely knows how to produce: it makes loss
*enumerable*, so after a disk failure the user knows exactly which files are
gone and which can be re-downloaded from public sources.

### 2. `./backups/<timestamp>/`, no retention

Predictable, zero config, works on a fresh clone, and takes no argument --
which is what gets a backup taken at all. `BACKUP_DIR=` redirects it.

The default is same-disk, and the documentation must say plainly that this
protects against user error and corruption but **not** drive failure, and that
the user should point `BACKUP_DIR` at external storage.

Retention is the user's job. Each run writes a new timestamped directory and
deletes nothing: automatic pruning is a feature whose bugs delete backups. The
script prints total size and directory count after each run so growth is
visible rather than surprising.

### 4. Version mismatch: record and warn

There is no migration framework in this repo. `services/ai/migration.py` is a
one-off for provider records, not a schema-version system. Beanie builds
indexes from the model classes at startup, but nothing versions documents or
transforms old shapes into new ones -- so a backup restored into a later app
version gets whatever tolerance the Pydantic models happen to have.

Restore therefore records the version and warns, attempting no transformation.
A mismatch requires `--force`. Refusing outright was rejected because it makes
an old backup useless at exactly the moment the user needs it most: after a
disk failure, on a reinstalled and newer app.

Building the migration framework now was also rejected -- with one release
line and no historical schema breaks, there is nothing to put in it. The
version stamp recorded here is precisely the input such a framework would need
later, so nothing is foreclosed.

**The contract, verbatim for `RESTORE.md`:** restore into the version you
backed up from; a mismatch warns and requires `--force`, attempts no
migration, and may fail on read if the schema changed.

### 5. No Fernet key in the backup

`app/services/ai/crypto.py` states its own threat model: Fernet defends
against "a look at the collection -- an opened Compass window, a stray
`mongodump` in a backup." A backup containing both the ciphertext and the key
that decrypts it is exactly the pairing the encryption exists to prevent, and
a backup is likelier than the live disk to reach a NAS or cloud sync.

The key lives at `$BIOINFO_HOME/.biopipe/secret.key`, separate from the Mongo
data, so excluding it is a matter of simply not copying it.

Recovery cost is re-pasting an API key -- one-time, at restore. A
`--with-secrets` flag was rejected: it invites the wrong choice at the moment
the user is least careful, and adds a second code path through the most
security-sensitive part of the script for a benefit measured in one paste. A
user who genuinely wants that pairing can `cp` the file as an explicit act.

The Fernet key is **never printed**. It is a machine-generated value that
means nothing without its ciphertext, and printing it would land it in
scrollback and terminal history -- undoing the decision. What the user
actually needs at restore is to know *why* their providers are broken and
*which* to fix, which `providers.txt` gives them without carrying anything
that decrypts anything.

## The backup directory

`make backup` writes `./backups/2026-08-17T134502Z/` containing five files.

### `dump/`

`mongodump` of the whole `biopipe` database, via
`docker compose exec -T mongo`. Every collection, no allowlist -- so a
collection added later is captured without anyone remembering to update the
script. An allowlist's failure mode here is silent and permanent.

### `data-manifest.tsv`

One row per blob: `blob_id`, `size`, `rel_path` or `external_path`,
`content_sha256`, `state`. Generated from `blobs`.

Written as a standalone file readable with `cut` and `grep` on a machine with
no Mongo, no Docker, and no BioFlow. That is why it exists separately from the
dump that already contains the same data: after a disk failure this is the
file that says what was lost, and it must be readable in the worst state the
machine will be in.

### `providers.txt`

Provider name, task slots, model, and the last four characters of each key.
No ciphertext, no Fernet key. Produced by a short read-only query through the
`api` container rather than reimplementing Fernet decryption in shell.

Written into the backup so it is readable *before* running restore, and
printed again by restore.

### `manifest.json`

App `VERSION`, git SHA, UTC timestamp, `BIOINFO_HOME` at backup time,
per-collection document counts, blob count, and total blob size.

The document counts are load-bearing: restore asserts against them after
loading, so a partial restore is detected rather than assumed.

### `RESTORE.md`

The contract, written into every backup rather than only living in the repo,
because the machine that needs it may not have the repo. States what restore
recovers (all metadata), what it does not (blobs in `/data`, provider keys),
and the version-mismatch rule.

## Restore

`make restore BACKUP=backups/2026-08-17T134502Z`, in order:

1. **Preflight.** Backup directory exists, all five files present,
   `manifest.json` parses, stack up and Mongo reachable. Fail before touching
   anything if not.
2. **Version check.** Compare `manifest.json`'s `VERSION` against the repo's
   `VERSION`. On mismatch print both, state that no transformation is
   attempted, exit non-zero unless `--force`. On match, proceed silently.
3. **Confirm.** Restore overwrites the live database. Unless `--force`,
   require the user to type the database name. Non-interactive without
   `--force` is a refusal, not a default-yes.
4. **`mongorestore --drop`.** Per-collection drop-and-load.
5. **Verify.** Re-count every collection against `manifest.json`. Any mismatch
   is a loud failure naming the differing counts, not a warning.
6. **Report.** Print the provider summary and the reminder that keys need
   re-entering, plus the blob count and a pointer to `backup-verify`.

Two limits stated plainly in `RESTORE.md` rather than buried:

- **Restore is not "reset to exactly this backup."** `--drop` is per
  collection, so a collection created after the backup was taken survives.
- **Restore does not verify `/data`.** It reports the blob count and stops.

## `make backup-verify`

`make backup-verify BACKUP=<dir>` walks `data-manifest.tsv` against the
current `/data` and reports missing files, size drift, and -- behind a flag,
since it is slow -- hash mismatches.

This satisfies the issue's second verification requirement (that the manifest
correctly identifies a file deleted out from under the app) and is useful
independent of any restore, as a periodic integrity check. Keeping it a
separate subcommand rather than a step inside restore keeps a slow operation
explicit.

## Testing

`ops/tests/test_backup_restore.py`, pytest, running in CI alongside the four
existing ops tests (`.github/workflows/build-check.yml:169`). No new CI wiring.

### Isolation is the critical detail

The test starts its own throwaway Mongo container on a random port, never the
running stack's. This is the trap `backend/run-worktree-tests.sh` exists to
avoid, and it is sharper here: a round-trip test's whole job is to drop a
database and reload it, so pointed at the wrong Mongo it destroys the user's
research record while reporting success.

The script takes its Mongo target from an environment variable so the test can
redirect it. **That indirection exists for the test and is the single most
important design detail in the file.**

### Round trip

Seed the scratch Mongo with a fixture spanning the shapes that matter: a
project; objects with roles and formats; blobs both managed and external; a
pipeline run with jobs; a `job_timings` row; an `ai_providers` document with
real Fernet ciphertext. Back up, drop the database, restore, then assert every
collection count matches, a provenance chain resolves end to end, and the
provider document round-trips as ciphertext.

### Assertions beyond the happy path

These catch the regressions that matter:

- **The backup directory contains no plaintext API key and no Fernet key** --
  grep the whole tree for both. This is what keeps the security decision true
  after someone edits the script a year from now.
- Version mismatch without `--force` exits non-zero and writes nothing.
- Version mismatch with `--force` completes.
- An injected doc-count mismatch between dump and verify fails loudly.
- `backup-verify` flags a blob deleted out from under it, and passes clean
  when nothing is missing.

### One manual pass

Before closing the issue: back up the real database, restore into a **scratch
stack, not the live one**, and confirm projects, provenance, and run history
are present. A fixture cannot catch a document shape only real data has.

## Files

| File | Change |
|---|---|
| `ops/backup.sh` | New. Subcommands `backup`, `restore`, `verify`. |
| `Makefile` | New targets `backup`, `restore`, `backup-verify`. |
| `ops/tests/test_backup_restore.py` | New. Round trip plus the assertions above. |
| `.gitignore` | Add `backups/`. |
| `README.md` or `docs/` | The restore contract and the same-disk caveat. |

## Risk

The script is straightforward; the test harness is where the implementation
risk sits. Standing up a throwaway Mongo, pointing the script at it, and
keeping that redirection airtight is most of the work and all of the danger.
Get the isolation right before writing any assertion.
