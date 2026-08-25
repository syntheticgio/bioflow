# Helper script for shared storage outside the launcher — implementation plan

Issue: [#849](https://github.com/syntheticgio/bioflow/issues/849)
Spec: `docs/superpowers/specs/2026-08-25-shared-storage-helper-script-design.md`

Status: **BLOCKED on #844.** Two of the acceptance criteria (the probe, its
remedy on failure) cannot be written against an endpoint that does not exist,
and guessing its shape means writing a `curl` line that has never run.

## Gate before any code

- **#844 merged**, and its probe endpoint's path, method, argument (node name
  vs id) and response shape read from the merged source, not from its spec.
- **#847's constants settled** — the share name and the account name. This
  script duplicates them (spec Q2) and the anti-drift test asserts against the
  launcher's source, so that source must exist. If #847 is deferred, this script
  becomes the *definition* of those constants and #847 mirrors it instead; say
  which way round in the commit body, because the anti-drift test's direction
  depends on it.

Not blocked on #848 — the `mount` half is a parallel implementation (Q2), not a
consumer of it.

## Commits

### 1. `feat(ops): add shared-storage.sh with platform detection and preconditions`

The script's skeleton and everything that refuses. No mutation at all yet — this
commit ships a script that can only say no, which is exactly the part worth
reviewing on its own.

- **New `ops/shared-storage.sh`.**
  - `#!/usr/bin/env bash`, then the header comment as a design document per
    `ops/` convention (`backup.sh:1-13` is the model). It must carry: why SMB
    rather than NFS (#843), why this duplicates #847/#848 rather than sharing
    code (spec Q2, and it should name `migrate-storage.sh:13-19` as the
    precedent), and the `Usage:` line with both subcommands.
  - `set -euo pipefail`.
  - Constants — share name, account name, mount option string, credentials file
    path — each with a `migrate-storage.sh:13-19`-style comment naming the Rust
    or Python site it mirrors. **Without those comments this is undocumented
    duplication rather than a decided one.**
  - `# --- dispatch ---` marker before the subcommand `case`. **Load-bearing:**
    every file in `ops/tests/` splits the script on such a marker to source its
    preamble (`test_backup_restore.py:38-40`, `test_worktree_prune.py:34-36`),
    and both assert the marker exists. Functions must be defined *above* it.
  - Positional subcommand, then `for arg` + `case` for `--yes`. No `getopts`.
  - `detect_platform`, `require_binary`, and the refusal messages (HS5-HS8),
    each `echo ... >&2; exit 1` inline — **no `die()` helper**, per convention.
- **New `ops/tests/test_shared_storage.py`** — layer 1 only in this commit:
  platform refusal and missing-binary refusal, with `uname` and `PATH` faked.
  Assert the macOS-`mount` message names `mount_smbfs`, per HS6.

### 2. `feat(ops): configure the primary's SMB share from shared-storage.sh`

The `share` subcommand.

- **`ops/shared-storage.sh`** — preconditions in `migrate-storage.sh` order:
  `.env` exists (`:40`), `BIOINFO_HOME` non-empty (`:46`), then the sentinel
  check (HS9) reading `$BIOINFO_HOME/.biopipe/VERSION`
  (`backend/app/config.py:632-634`), then the existing-share inspection.
- The printed plan and the `[y/N]` gate (HS3/HS4), `--yes` to skip.
- macOS: `sharing -a`/`sharing -e` with **`-g 000`** and `-E 1`. `-g` defaults to
  guest-access ON (`man 8 sharing`, and `sharing -l` on a stock machine reports
  `guest access: 1`) — omitting it ships a world-readable share, which is the
  single worst mistake available in this ticket.
- Linux: the `[bioflow]` stanza and `smbpasswd`, password on stdin (HS18).
- The account creation, disclosed in the plan by name.
- Credential generation gated on the backend reporting none stored (HS11).
- Final line names the next command (HS19).
- **`ops/tests/test_shared_storage.py`** — the `sharing -l` parser and the
  idempotency decision (HS10/HS11), with a **fixture captured from a real
  machine**. A hand-written fixture that does not match Apple's format makes
  every test here vacuous. Plus layer 2: run `share` for real with stdin closed
  against a tmp_path `.env` and assert no mutation (HS3/HS4) — layer 1 cannot
  reach this, per `test_backup_restore.py`'s #492 note.

### 3. `feat(ops): mount the primary's share on a Linux node from shared-storage.sh`

The `mount` subcommand. Separate commit: different machine, different code path,
independently revertable.

- **`ops/shared-storage.sh`** — Linux-only guard, `mount.cifs` check, mountpoint
  and fstab preconditions, the plan, the credentials file at mode `0600`
  (HS17), the fstab write, `mount -a`.
- The **fstab decision as its own function** — match by mountpoint, return
  append/replace/no-op/conflict. Keep it above the dispatch marker so layer 1
  can test it.
- **`ops/tests/test_shared_storage.py`** — the fstab decision table
  (HS12/HS13). The `Replace` case with differing options is the one that
  matters: `Append` passes a naive presence check and only manifests at the
  node's next reboot.

### 4. `feat(ops): run the storage probe from shared-storage.sh and fail on a red result`

The acceptance criterion the script exists to satisfy. Last, because it is the
only commit that depends on #844's merged shape.

- **`ops/shared-storage.sh`** —
  - `share`: `docker compose -p biopipe --project-directory "$REPO_ROOT" exec -T api /usr/local/bin/python3.12 -c ...`.
    The interpreter path is literal and explicit (HS16); `python`/`python3` in
    that container are the medaka venv and fail with a missing-module error that
    names the wrong cause. `-p biopipe --project-directory` follows
    `migrate-storage.sh:58` and is what keeps a worktree invocation from
    repointing the shared 5173 stack.
  - `mount`: `curl -fsS` against #844's probe endpoint. `-f` is what makes a
    non-2xx a non-zero exit (HS14).
  - A red probe exits non-zero **with the remedy** (HS15), not a warning.
  - `share` with the stack down: skip, report, print the command — do not fail.
- **`ops/tests/test_shared_storage.py`** — HS16's grep assertion (no bare
  `python3`/`python -m` in an exec line). Crude, and it prevents an hour of
  diagnosis.

### 5. `docs(ops): point at shared-storage.sh from node setup`

HS20, and it must land with commit 3 or the script is unfindable.

- **`README.md`** — a shared-storage section. Note that `README.md` currently
  mentions nodes only at `:126` (a database note), so this needs a real home;
  find where node setup is described and if nowhere, that is itself the finding.
- **`docs/superpowers/specs/2026-08-10-multi-node-design.md`** — check whether
  its deferred "Phase 2 cross-node data transfer" claim is now false. If so it
  changes here or it starts lying.
- **`docs/TODO.md`** — if an entry covers this, append ` — FIXED` with what
  shipped and move the whole entry to `docs/TODO-done.md`, per CLAUDE.md.

### 6. `test(ops): assert the share constants match the launcher and backend`

Q2's mitigation, and the only thing supporting its "drift is bounded" claim.
Separate commit so it can be reverted if #847's constants move, without taking
the script with it.

- **`ops/tests/test_shared_storage.py`** — read the share name and account name
  out of `launcher/src-tauri/src/share.rs` and the backend's stored-credential
  model, and assert they equal the script's constants. Skip with a clear reason
  if #847 has not landed, rather than failing — a red check for a dependency
  that does not exist yet teaches nothing.

## What else changes, or starts lying

- **`ops/tests/` gains a file.** Per GROUND.md section F and the existing seven,
  a new `ops/*.sh` without one is an incomplete change.
- **The `# --- dispatch ---` marker** is a contract with `ops/tests/`, not a
  comment. Moving it breaks the test with an explicit assertion message, which
  is the design.
- **`README.md`** and possibly `docs/superpowers/specs/2026-08-10-multi-node-design.md`
  — commit 5.
- **`launcher/src-tauri/src/share.rs`** — not modified here, but commit 6 reads
  it. If #847 renames the share, this test goes red, which is the point.
- **`docs/manual-checks/`** — the real mount, a wrong-credential refusal, and a
  green probe cannot be automated and belong there.

## Verification

- `./backend/run-worktree-tests.sh ops/tests/test_shared_storage.py -q` — from
  the worktree. **Not `docker compose exec api`**, which tests main's code.
- The whole `ops/tests/` directory, not just the new file — commit 6 reads other
  sources and can break on their movement.
- `ruff check --config backend/pyproject.toml backend/app backend/tests ops e2e`
  from the repo root. `ops/tests/` is in ruff's scope.
- `bash -n ops/shared-storage.sh` — a syntax check the pytest layers do not
  give, since layer 1 only sources the preamble.
- **`shellcheck ops/shared-storage.sh`** if available. Not a CI check and not a
  gate, but this script quotes paths into system commands and that is where
  shellcheck earns its keep.
- **Manual, on two real machines, and nothing else proves these:**
  - `share` on the macOS primary; `sharing -l` reports `guest access: 0`.
  - `share` twice: one share record, credential unchanged.
  - `mount` on a Linux node; `mount -a` after a reboot still mounts once.
  - `mount` twice: one fstab line. Then hand-edit its options and re-run —
    assert it is *replaced*, not appended.
  - A wrong credential is refused. "It mounted" passes on a guest share.
  - The probe green, and — deliberately — the probe **red** after unmounting,
    confirming the non-zero exit and the remedy text (HS14/HS15). A path only
    ever exercised green is a path never exercised.
  - `./ops/shared-storage.sh mount` on macOS: the refusal message, checked for
    the `mount_smbfs` explanation.

## Out of scope

- #847 (the launcher), #848 (mount during provisioning), #850 (teardown).
- macOS compute nodes.
- Installing Samba or `cifs-utils` — refuse with the command, matching
  `verify_docker`'s "Install Docker first" posture at
  `backend/app/api/v1/nodes.py:768-777`.
- Credential rotation.
- Reimplementing the probe in bash (spec Q3).
