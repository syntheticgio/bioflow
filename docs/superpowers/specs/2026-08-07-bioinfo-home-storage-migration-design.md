# Migrate BIOINFO_HOME storage location

Issue: [#76](https://github.com/syntheticgio/bioflow/issues/76)
Parent epic: [#29 — Migrate volume](https://github.com/syntheticgio/bioflow/issues/29)
Depends on: [#75 — Move Mongo/Redis into ~/.bioflow](https://github.com/syntheticgio/bioflow/issues/75) (shipped)

## Problem

The launcher's Settings screen already lets a user change `BIOINFO_HOME`
(`launcher/src/Settings.tsx`), but it only rewrites `.env` and recreates the
stack — it does not move any existing data, and the UI warns about this at
the point of change (`storageLocationChanged` in
`launcher/src/wizard-logic.ts`).

This issue makes "change storage location" actually migrate the data, for
both ways this app runs: through the launcher, and by hand with plain
`docker compose` (this repo's own dev-trunk setup, see
[CLAUDE.md](../../../CLAUDE.md)). Now that #75 has moved Mongo/Redis into
the fixed `~/.bioflow` directory, this migration only ever has to move one
thing: the `BIOINFO_HOME` folder's contents.

## Non-goals

- No change to Mongo/Redis storage — that is #75, already shipped. This
  issue's UI must not imply the operation moves "everything BioFlow owns."
- No general/repeatable migration framework beyond what's described here —
  single-user, single-machine tool, per CLAUDE.md.
- No support for migrating *while the stack is running* — the stack must be
  stopped first, both because copying open files is unsafe and because
  `LauncherState::Stopped` is the gate this flow is offered from.
- No cross-machine or cross-user migration (e.g. via network share) —
  source and destination are both local paths on the same machine.

## Design

### Where the copy runs

Directly in the launcher process (Rust), not inside a throwaway container.
The launcher process runs as the same OS user that owns `BIOINFO_HOME` on
disk and already has full filesystem access to both the old and new paths —
there is no permission boundary a container would cross that the launcher
process doesn't already have. This also keeps the implementation in one
place (Rust) rather than splitting logic between Rust and a shelled-out
container invocation.

### Entry point: a distinct dialog, not the passive Settings field

The existing Settings storage-location field keeps its current behavior
unchanged (instant repoint, no copy, existing data-loss warning) — it stays
available as an escape hatch, e.g. for a user who already moved data by
hand and just needs `.env` updated.

A new, separate "Migrate storage location…" action is shown only when
`LauncherState::Stopped` (installed, Docker up, stack not running — the
exact state the issue's "detects an installation... but not running"
describes). It opens its own dialog:

- Current location shown read-only.
- A destination path picker/input for the new location.
- Two checkboxes, both described below:
  - **"Keep the original copy"** (default: unchecked → the original *is*
    deleted after a successful, validated copy).
  - **"Validate by hash (this may take hours depending on the size of the
    data)"** (default: unchecked → validation is count+size only).
- A "Start migration" button that runs the flow below.
- Progress display: `X GB / Y GB (Z%)` — bytes copied so far, total bytes
  (from a pre-copy directory walk), and the percentage, computed as
  `bytes_copied / total_bytes * 100` (per user direction: a plain division,
  not a smoothed or estimated progress).
- No cancel button (see #75's sibling brainstorming decision for this
  issue: coarse progress without cancel was chosen over the added
  complexity of interrupting and rolling back a partial copy).

### Migration flow

Given a chosen destination path, in order:

1. **Validate the destination.** Reuse
   `setup::validate_storage_path` (`launcher/src-tauri/src/setup/validate.rs`)
   — the same writable + macOS Docker-file-sharing check first-run setup
   already runs on a storage path. If this fails, stop before touching
   anything; show the same `StoragePathValidationDto` messaging the setup
   wizard uses (`NotWritable` / `NotDockerShared`).

2. **Walk the source directory once** to compute total file count and total
   byte size. This total drives both the progress display and the disk
   space check — one walk serves both, no need to walk twice.

3. **Check free space at the destination.** Refuse to start if
   `free_space_at_destination < total_source_bytes + 100_GB`. The 100GB
   figure is a flat margin (not a percentage of source size), chosen so the
   destination volume is never left running BioFlow, Docker, and the OS
   itself with headroom in the single-digit gigabytes after a migration —
   independent of how large or small the copied data happens to be. Report
   this as a clear pre-flight error (needed vs. available) rather than
   letting the copy start and fail partway through.

4. **Copy.** Recursively copy the source directory tree to the destination,
   preserving permissions and symlinks (`cp -a` semantics on macOS/Linux;
   the launcher is macOS/Linux-targeted per `launcher/src-tauri/tauri.macos.conf.json`
   and `tauri.linux.conf.json` — no Windows-specific copy semantics are in
   scope). Report progress via the running byte count from step 2's file
   list as each file completes.

5. **Validate the copy.**
   - Default (fast): compare file count and total byte size between source
     and destination against the values captured in step 2.
   - If "validate by hash" is checked: additionally hash every file on both
     sides and compare, file by file. This is the slow path the checkbox's
     own label warns about.
   - If validation fails, stop here. Do **not** delete the source, and do
     **not** update `.env`/restart the stack against the new (unverified)
     location. Report exactly what failed (e.g. "142 files copied, 145
     expected" or the specific file(s) whose hash didn't match).

6. **Update `.env` and restart the stack against the new location**, reusing
   the existing `settings::apply` (`launcher/src-tauri/src/settings.rs`) —
   this is precisely what that function already does for a plain repoint,
   so no new stack-restart logic is needed here, only a new caller that
   invokes it after the copy+validate steps above instead of instead of
   them.

7. **Delete the original directory**, unless "keep the original copy" was
   checked. This is the last step, gated on validation having already
   passed in step 5 — not on the restarted stack coming up healthy, since
   step 5's validation is what actually proves the data is intact; waiting
   on stack health would conflate "did the copy succeed" with "does the
   application start," which is a separate concern already handled by the
   existing run/health-check flow. Deletes the whole directory (not just
   its contents), matching the mental model of "a single directory that
   held the storage, now empty of purpose."

### Failure handling

- A failure at steps 1–3 (validation, walk, space check) leaves nothing
  touched — no partial copy exists yet.
- A failure at step 4 (copy interrupted, disk fills mid-copy, permission
  error partway through) must not proceed to step 5's validation passing by
  construction (an incomplete copy will fail the count/size check) — but
  explicitly, do not delete the source and do not touch `.env` if step 4
  itself errors out before completing.
- A failure at step 5 (validation) stops before step 6/7, per above.
- A failure at step 6 (the `settings::apply` restart) is already handled by
  that function's existing `SettingsUpdateError` — `.env` is written but the
  stack failed to recreate. In this flow, this must **not** trigger step
  7's deletion: deleting source data because the *stack restart* failed,
  when the *copy* was already proven correct, would destroy a good copy
  over an unrelated failure. Report the restart error and leave both the
  new location's data and the original intact; the user can retry starting
  the stack (e.g. via the normal Run action) without re-copying anything.

### Manual/by-hand path: a standalone script

For the dev-trunk case (this repo's own `docker compose up` workflow, which
is not the launcher) and any non-launcher `docker compose` user, add
`ops/migrate-storage.sh <new-path>` implementing the same sequence as
above, minus the launcher-specific UI:

1. Validate the destination is writable (create it if missing).
2. Check free space at the destination against the source directory's total
   size plus the 100GB margin; refuse to proceed if insufficient.
3. Require the stack to be stopped first (refuse to run while
   `docker compose ps` shows running containers, mirroring
   `LauncherState::Stopped`'s intent) — print the exact stop command if not.
4. Copy (`cp -a` or `rsync -a`) with progress output to the terminal (bytes
   copied / total / percentage, matching the launcher's own progress
   semantics for consistency between the two paths, even though this is a
   plain terminal script rather than a GUI).
5. Validate: count + size by default; a `--verify-hash` flag opts into the
   slow hash-based check, printing the same "this may take hours" warning
   the launcher checkbox carries.
6. On success, update `BIOINFO_HOME` in `.env` and print the command to
   restart the stack (`docker compose up -d --build api web worker`, this
   repo's own standing rebuild command) rather than running it automatically
   — the script should not restart containers out from under a user who may
   be mid-session with the shell it's running in.
7. Delete the original directory unless `--keep-original` is passed
   (mirroring the launcher's checkbox, defaulted the same way: delete by
   default).

This keeps the two paths' logic in sync by design (same steps, same
defaults, same 100GB margin) without sharing code across Rust and bash —
duplicating a well-specified sequence of shell/filesystem operations is
cheaper here than building a shared abstraction for two callers on two
different runtimes.

### Metadata note

Per #75, Mongo/Redis already live in the fixed `~/.bioflow` directory and
are entirely out of scope for this migration. The dialog and script must
not describe this operation as moving "everything" — only the
`BIOINFO_HOME` storage folder.

## Testing

- Rust unit tests for the copy/validate/space-check logic against temp
  directories (no real Docker/launcher state needed) — mirroring how
  `settings.rs` and `setup/validate.rs` already test their logic against
  `tempfile::tempdir()`.
- A full manual run against a real (non-trivial-sized) `BIOINFO_HOME` on
  this machine: migrate to a new path via the launcher dialog, confirm
  progress numbers look sane, confirm the destination stack starts and
  existing projects/files are visible, confirm the original directory was
  removed (or preserved, if "keep the original copy" was tested).
- A full manual run of `ops/migrate-storage.sh` against this machine's real
  dev-trunk `BIOINFO_HOME`, exercising both the default (count+size) and
  `--verify-hash` validation paths.
- A deliberately-failed run (e.g. destination path with insufficient free
  space, or a destination that isn't writable) to confirm the pre-flight
  checks refuse to start rather than beginning a copy that can't finish.
