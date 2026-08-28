# Helper script for shared storage outside the launcher — design

Date: 2026-08-25.

Addresses [#849](https://github.com/syntheticgio/bioflow/issues/849). Child 6 of
[#843](https://github.com/syntheticgio/bioflow/issues/843).

**Depends on [#844](https://github.com/syntheticgio/bioflow/issues/844)** — the
probe is an acceptance criterion, not an optional extra.

## What exists today

Verified against this worktree on 2026-08-25.

### `ops/` has a strong, consistent shape

Six `.sh` files: `backup.sh`, `check_tag_matches_version.sh`,
`migrate-storage.sh`, `release.sh`, `test-timing.sh`, `worktree-up.sh`. All
share:

- `#!/usr/bin/env bash`, then a **header comment that is a design document** —
  5-15 lines of prose on why the script exists, linking a spec. `backup.sh:1-13`
  is the model, and it spends four of its lines on a security decision (why the
  Fernet key is never read).
- `set -euo pipefail` immediately after the header.
- A `Usage:` line in the header.
- **Positional args first, then `for arg` + `case`.** No `getopts` anywhere.
  `migrate-storage.sh:20-30`.
- **No `die()` helper.** Errors are inline `echo ... >&2; exit N`, and the
  message contains the literal remedy command —
  `migrate-storage.sh:56-63` prints the exact `docker compose ... down`.
- **Guard-and-exit before any mutation.** `migrate-storage.sh` checks, in order:
  `.env` exists (`:40`), `BIOINFO_HOME` non-empty (`:46`), not already at target
  (`:51`), stack not running (`:56-63`), source exists (`:65`), free space
  (`:80`). Only then does it copy. It then **verifies before destroying**
  (`:105`) and gates the destructive step behind `--keep-original`.
- **Scripts end by printing the next command** — `migrate-storage.sh:157-159`.

### The duplicated-constant precedent

`migrate-storage.sh:13-19`:

```
# Flat margin required at the destination beyond the source's own size --
# matches MIGRATION_SPACE_MARGIN_BYTES in launcher/src-tauri/src/migrate.rs.
# Kept as a duplicated constant rather than a shared file: this is a bash
# script and that is Rust, and the two runtimes have no shared config file
# to source from without inventing one for a single number.
```

The repo has already decided, explicitly, that duplication beats invented shared
config **when the duplicated thing is a single value with a named source**.
Decision Q2 turns on whether that condition holds here.

### `ops/tests/` and its three layers

Seven test files: `test_backup_restore.py`, `test_bump_version.py`,
`test_compose_target.py`, `test_release_preflight.py`, `test_tag_guard.py`,
`test_worktree_prune.py`, `test_worktree_test_volume_leak.py`. A new `ops/*.sh`
is expected to ship with one.

The pattern, from `test_backup_restore.py:1-15` and `test_worktree_prune.py:26-46`:
tests read the script's text, split on a **dispatch marker** comment, and
`bash -c` only the preamble — the function definitions — with `set -euo pipefail`
rewritten to `set -uo pipefail`. `test_backup_restore.py`'s docstring names the
trap this creates:

> #492 found that layer 1 sources only the preamble above the dispatch `case`,
> so nothing *inside* a subcommand was executed by any test that runs in CI.

Hence its three layers: pure sourced functions, real execution of a subcommand
that needs no service, and a Docker-gated round trip.

### The probe's runtime

#844's probe is Python, in the `api` container. Per CLAUDE.md, inside that
container `python` and `python3` resolve to a tool venv without the app's
dependencies; `/usr/local/bin/python3.12` is the interpreter.

## Decision Q1: one script, two subcommands

`ops/shared-storage.sh`, dispatching on `$1`:

```
./ops/shared-storage.sh share                    # on the primary
./ops/shared-storage.sh mount <primary-host>     # on a Linux node
```

**Not two scripts.** Both ends share more than they differ: the share name, the
username, the mount path convention, the credential format, the probe
invocation, and the "state what you will change" preamble. Two files would
duplicate all of it, and the constants are exactly the kind that drift.

**Not a `--mode` flag.** `ops/` has no `getopts` and its multi-mode script,
`backup.sh`, already dispatches on a positional subcommand with a
`# --- dispatch ---` marker. A `--mode=` flag would be a third convention in a
directory with two.

The subcommand form also matches how the two halves are actually used: never on
the same machine, never in the same session, by (potentially) different people.

`share` runs on macOS or Linux; `mount` is **Linux only**, per the epic's
scope boundary (#843: "Linux nodes only").

## Decision Q2: a parallel implementation, and the drift is real but bounded

The script does **not** shell out to #847/#848, and #847/#848 do not shell out
to it.

**Why not shell out from the launcher.** The launcher's whole value in #847 is
the OS authorization dialog. Shelling out to a bash script means the elevation
happens some other way — a terminal `sudo`, which is precisely what the issue
says the launcher exists to avoid. And `ops/shared-storage.sh` lives in a git
checkout that a launcher-installed user does not have; the launcher installs to
`~/.bioflow`, not a repo.

**Why not shell out from the script to the backend.** The `share` half runs
before any node exists and configures the host, not a node. #848's mount logic
lives inside `_provision_node`'s SSH session (`backend/app/api/v1/nodes.py:671`)
and is expressed as `RemoteStep`s against an `asyncssh` connection — there is no
callable surface a bash script could reach.

**So: parallel implementation, and it will drift.** Being honest about that is
better than a shared-code story that does not survive contact.

Against the `migrate-storage.sh:13-19` precedent: that precedent covers a
**single constant with a named source**. This is a *procedure*, which is a
weaker case for duplication. What makes it acceptable anyway:

- The duplicated surface is small and slow-moving: a share name (`bioflow`), a
  username (`bioflow-share`), a mount option string, and a credential-file
  format. Four values, not four hundred lines.
- Three of the four are **externally observable and cross-checkable**. A
  divergent share name means the node cannot mount, which #844's probe catches
  immediately and loudly. This is the "keys owned outside this repo" case from
  CLAUDE.md's registry rule: a bad value fails elsewhere, visibly.

**Mitigation, and it is required rather than nice-to-have:** each duplicated
constant carries a `migrate-storage.sh`-style comment naming the Rust or Python
site it mirrors, and a test in `ops/tests/` asserts the share name and username
match the values in the launcher and backend sources by reading both files. That
test is cheap, needs no SMB, and is the only thing standing between this decision
and silent drift.

## Decision Q3: `docker compose exec` the probe on the primary; `curl` the API from the node

The two halves are in different positions and need different answers. One
mechanism for both would be wrong for one of them.

**`share` (on the primary).** The repo checkout and a running stack are both
present — that is the definition of this script's audience. Run the probe the
way CLAUDE.md prescribes:

```bash
docker compose -p biopipe --project-directory "$REPO_ROOT" exec -T api \
  /usr/local/bin/python3.12 -c '...'
```

Explicitly `/usr/local/bin/python3.12`, never `python` or `python3` — inside the
`api` container those are the medaka venv, and the failure is an import error
that names a missing module rather than the wrong interpreter. `-T` because
there is no TTY in a script. `-p biopipe --project-directory` follows
`migrate-storage.sh:58`, which is also what keeps this from repointing a
worktree's stack (CLAUDE.md's hook blocks bare `docker compose` from a
worktree).

If the stack is not running, `share` does not fail — it configures the share and
reports that the probe was skipped, naming the command to run once the stack is
up. Refusing to configure a share because a container is down would be a guard
that blocks the wrong thing.

**`mount` (on the node).** The node has **no repo checkout and no `api`
container.** `docker compose exec` is not available. Use the HTTP endpoint:

```bash
curl -fsS -X POST "http://${PRIMARY_HOST}:8000/api/v1/nodes/${NODE}/storage-probe"
```

— #844's re-runnable probe endpoint, which its acceptance criteria require
("The probe is re-runnable against an already-enrolled node"). `-f` makes a
non-2xx a non-zero exit, which is exactly the acceptance criterion here.

**Rejected: reimplement the sentinel round trip in bash.** It is four lines
(write a UUID into `$BIOINFO_HOME/.biopipe/`, read it at the mountpoint, delete)
and that is what makes it tempting. It is a third implementation of the one
thing #844 exists to be the single source of truth for, and a bash version that
subtly diverges — different filename, no cleanup on failure — would report
green while the real probe reports red. The whole epic's ordering ("probe first,
automate second") is an argument against this.

**Consequence for `mount`, stated plainly:** the node must already be enrolled
for the probe endpoint to have a node to probe. So `mount` takes the node's name
or id as an argument and, when it is absent, does the mount and prints the exact
probe command to run afterwards rather than exiting zero on an unproven mount.

## Decision Q4: detect with `uname -s` plus an `/etc/os-release` check, and refuse before touching anything

Detection runs in the preconditions block, before any mutation.

**`share`:**

- `Darwin` → the `sharing`/`launchctl` path. Additionally require
  `/usr/sbin/sharing` to exist; it is stock, but a check costs nothing and the
  alternative failure is "command not found" mid-sequence.
- `Linux` → the Samba path. Require `smbd` or the `samba` package to be
  installable; if `smbpasswd` is absent, that is a refusal with the install
  command for the detected distro, not an attempted install.
- Anything else → refuse.

**`mount`:** `Linux` only. `Darwin` gets its own message, because a macOS user
running this will have a specific wrong expectation:

```
This script mounts the share on Linux nodes only.

macOS nodes need mount_smbfs and a LaunchAgent instead of cifs-utils and
/etc/fstab -- a different client implementation, deliberately out of scope
(see issue #843). There is no supported path for a macOS compute node yet.
```

Requiring `mount.cifs` is a separate check with its own message
(`apt install cifs-utils` / `dnf install cifs-utils`), because "wrong OS" and
"right OS, missing package" are different problems with different remedies and
one message covering both helps neither.

Every refusal is `echo ... >&2; exit 1`, inline, per `ops/` convention.

## Decision Q5: guard-and-exit, then a printed plan, then mutate

`migrate-storage.sh`'s model, applied to both subcommands.

**Preconditions, all before any change:**

`share` — platform supported (Q4); the required binary present; `.env` exists
and `BIOINFO_HOME` is non-empty (`migrate-storage.sh:40-49` is the exact
pattern); `$BIOINFO_HOME/.biopipe/VERSION` exists, because on macOS an unmounted
external volume presents as an empty directory
(`backend/app/storage/home.py:1-11`) and sharing it exports nothing; no
conflicting `bioflow` share for a *different* path.

`mount` — Linux; `mount.cifs` present; the mountpoint is either absent or an
empty directory; no existing `/etc/fstab` line for this mountpoint; the primary
is reachable on 445.

**Then the plan.** Before the first mutation, print exactly what will change and
require confirmation:

```
This will change the following on this machine:

  * Create share point 'bioflow' exporting /Volumes/FastDataExtension/BioinfoHelper
  * Create system account 'bioflow-share' (no login shell, no admin group)
  * Enable the SMB service (com.apple.smbd)

Continue? [y/N]
```

`--yes` skips the prompt for unattended use. Nothing else is promptable — a
script that asks six questions is a script people answer wrong.

**Idempotency, per acceptance criterion:**

- `share` — an existing `bioflow` share for the same path is reconciled
  (flags corrected), not duplicated. An existing account keeps its password
  untouched. A credential is generated **only if none is stored**; the script
  asks the backend first, for the same reason #847 does (Q7 of that spec): you
  cannot un-rotate a password already set.
- `mount` — the `/etc/fstab` line is matched **by mountpoint**, not by whole-line
  equality. A line differing only in options must be *replaced*, not appended;
  appending is how you get two entries for one mountpoint and a boot that mounts
  whichever `mount -a` reaches first. The credentials file is rewritten only
  when the credential actually differs.

**A conflicting existing state is an error, not a silent overwrite.** A
`bioflow` share pointing at a different path, or an fstab line for a different
server, exits non-zero naming what it found.

## Decision Q6 (from the ticket's docs criterion): the script documents itself, and README points at it

The header comment is the design document, per `ops/` convention, and it links
this spec. `README.md` gains a shared-storage pointer in the same commit — the
acceptance criterion is explicit that docs point at the script, and a script
nobody can find is a script nobody runs.

## Requirements

Permanent IDs; never reused.

- **HS1.** A user on a primary can configure the `bioflow` SMB share by running
  `./ops/shared-storage.sh share`.
- **HS2.** A user on a Linux node can mount the primary's share by running
  `./ops/shared-storage.sh mount <primary-host>`.
- **HS3.** The script prints every system-level change it will make before
  making the first one.
- **HS4.** The script makes no system-level change without either a confirmed
  prompt or `--yes`.
- **HS5.** The script refuses, with a non-zero exit, when `uname -s` reports a
  platform the requested subcommand does not support.
- **HS6.** The refusal message for an unsupported platform names why, and does
  not suggest a workaround that does not exist.
- **HS7.** The script refuses, with a non-zero exit, when a required binary
  (`sharing`, `smbpasswd`, `mount.cifs`) is absent, naming the install command.
- **HS8.** Every refusal happens before any system-level change.
- **HS9.** `share` refuses when `$BIOINFO_HOME/.biopipe/VERSION` does not exist.
- **HS10.** Running `share` twice does not create a second share point.
- **HS11.** Running `share` twice does not change an already-stored credential.
- **HS12.** Running `mount` twice does not add a second `/etc/fstab` entry for
  the same mountpoint.
- **HS13.** `mount` replaces, rather than appends to, an existing `/etc/fstab`
  entry for the same mountpoint whose options differ.
- **HS14.** The script runs #844's probe and exits non-zero when it fails.
- **HS15.** A failing probe's output includes the remedy, not only the failure.
- **HS16.** `share` invokes the probe through
  `/usr/local/bin/python3.12` inside the `api` container, not `python` or
  `python3`.
- **HS17.** The credentials file written on the node is mode `0600`.
- **HS18.** No credential appears in the script's stdout, stderr, or in any
  process's command-line arguments.
- **HS19.** The script's final output names the next command to run.
- **HS20.** `README.md` links to the script from where node setup is described.

## Testing

`ops/tests/test_shared_storage.py`, following the three-layer split
`test_backup_restore.py:1-15` documents — and the script needs a
`# --- dispatch ---` marker for layer 1 to source against, since that marker is
what every existing `ops/tests` file splits on.

**What can be asserted without an SMB server — which is most of it:**

- **HS5/HS6, platform refusal.** Source the preamble, call the platform check
  with `uname -s` faked to `Darwin`, `Linux`, `FreeBSD`. Assert the exit code
  and that the macOS-`mount` message mentions `mount_smbfs`. This is pure logic
  and needs nothing.
- **HS7, binary checks.** Same, with `PATH` pointed at an empty directory.
  Assert the message contains the literal install command.
- **HS12/HS13, the fstab decision.** The highest-value test here. A pure
  function over an fstab file's text and a desired entry, returning
  `Append | Replace{lineno} | NoOp | Conflict`. Feed it a real-shaped fstab with
  a matching mountpoint and differing options and assert `Replace` — because
  `Append` is the bug, it passes a naive "is the entry present" check, and it
  only manifests at the node's next reboot.
- **HS10/HS11, share idempotency.** A pure function over `sharing -l` output.
  Use a fixture captured from a real machine; a hand-written one that does not
  match Apple's actual format makes every parser test vacuous.
- **HS3/HS4, the plan-then-confirm gate.** Layer 2 — run the subcommand for real
  with stdin closed, against a tmp_path `.env`, and assert it exits without
  mutating. This is the layer #492 showed layer 1 cannot reach: the confirmation
  is *inside* the dispatch, so a preamble-only test never executes it.
- **HS16.** Grep the script for `python3.12` and assert no bare
  `exec .* python3 ` or `python -m`. Crude and effective; this is a mistake that
  costs an hour to diagnose and one second to prevent.
- **HS18.** Assert no `echo`/`printf` of the credential variable, and that it
  reaches `smbpasswd` on stdin rather than as an argument.
- **Q2's anti-drift test.** Read the share name and username out of
  `launcher/src-tauri/src/share.rs` and the backend, and assert they equal the
  script's constants. Without this, Q2's "drift is bounded" claim is
  unsupported.

**What cannot be, and is therefore a manual check:** the actual mount, the
credential being accepted, and the probe going green. Those need two machines
and belong in `docs/manual-checks/`.

## Verify before implementing

1. **#844's probe endpoint path, method, and response shape.** HS14 and the
   `mount` half's `curl` both depend on it. Written while #844 is in flight —
   confirm before writing the invocation.
2. **Whether the probe endpoint accepts a node name or requires a node id.**
   Determines `mount`'s argument.
3. **The `smb.conf` stanza that actually works** on the target distros, and
   whether `smbpasswd -a` reads a password from stdin without a TTY.
4. **The `mount.cifs` option string**, specifically the `uid=`/`gid=` values.
   #843 chose SMB partly because these sidestep UID mapping — the values must
   match the container UID the worker runs as, not the node user's.
5. **Whether `fcntl` locking over CIFS is safe** for
   `$BIOINFO_HOME/.biopipe/lock` (`backend/app/config.py:637-638`, taken via
   `fcntl` at `backend/app/storage/home.py:15`). Shared with #847's spec; it
   blocks #848 more than it blocks this, but a script that mounts a share the
   worker then locks badly is a script that shipped a corruption path.
6. **Where in `README.md` node setup is described.** It currently mentions nodes
   only at `:126`, in a database note — HS20 may need a section rather than a
   line.

## Out of scope

- **Replacing #847/#848.** Explicitly not (Q2); this is the same operations for
  people outside the launcher path.
- **macOS compute nodes.** #843's scope boundary.
- **Unmounting on the node.** #850.
- **Installing Samba or `cifs-utils`.** Refuse with the command (HS7), matching
  `verify_docker`'s posture at `backend/app/api/v1/nodes.py:768-777`, which
  checks for Docker and fails with "Install Docker first" rather than installing
  it. That is the established stance for root-requiring setup.
- **Credential rotation.**
- **Reimplementing the probe** (Q3).
