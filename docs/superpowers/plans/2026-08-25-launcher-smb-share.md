# Configure the SMB share on the primary from the launcher — implementation plan

Issue: [#847](https://github.com/syntheticgio/bioflow/issues/847)
Spec: `docs/superpowers/specs/2026-08-25-launcher-smb-share-design.md`

Status: **BLOCKED on two decisions and one dependency.**

## Gate before any code

1. **#844 must be merged.** The probe is the only proof the share works. Without
   it this ships an unverified share.
2. **The user must accept Q1 (split into #847a macOS / #847b Linux).** This plan
   implements #847a only. If the split is rejected, add commits 8-9 below.
3. **The user must accept Q4 (the launcher creates a `bioflow-share` system
   account).** If rejected, this plan is void and the spec's
   detection-and-instruction fallback replaces it — a much smaller ticket.

Then answer spec items 1-4 under "Verify before implementing" on a real machine
before writing the privileged script. Item 2 in particular: the `dscl` sequence
is the single largest unknown, and finding out it needs six commands instead of
two changes the shape of commit 4.

## Commits

Ordered so each is separately revertable, and so nothing privileged runs until
the pure logic under it is tested.

### 1. `feat(launcher): report whether the primary is sharing its data folder`

The whole of Q6, with no ability to change anything. Ships useful on its own: it
tells a user with a hand-configured share that BioFlow can see it.

- **New `launcher/src-tauri/src/share.rs`.** Header comment in this repo's style
  — why SMB, why live queries, linking the spec. Contents:
  - `pub enum ShareState { Off, On { path: PathBuf }, OnButUnmounted { path: PathBuf }, DaemonDown { path: PathBuf } }`
  - `pub trait SharePlatform` with `state`/`enable`/`disable` (the Q1 seam;
    `enable`/`disable` land in commit 4 — declare them now or the trait changes
    shape mid-series).
  - `pub fn derive_state(shares: &str, daemon_loaded: bool, sentinel_exists: bool) -> ShareState`
    — the pure function, taking already-captured command output. This is what
    the tests in this commit cover.
  - A `MacosShare` impl whose `state()` shells out to
    `sharing -l -f json` and `launchctl print system/com.apple.smbd`, then calls
    `derive_state`. Neither command needs elevation.
- **`launcher/src-tauri/src/lib.rs:1-10`** — add `pub mod share;` to the module
  list.
- **`launcher/src-tauri/src/lib.rs:16-46`** — register a new
  `commands::share_status` in `tauri::generate_handler![...]`. **A command not
  listed here is silently unreachable from the frontend** — this is the same
  class of hand-maintained registry CLAUDE.md warns about.
- **`launcher/src-tauri/src/commands.rs`** — `share_status` command, in the
  `spawn_blocking` style every Docker-touching command in this file already uses
  (see `install_dir_str_blocking`, `:128`). It must read `BIOINFO_HOME` from
  `~/.bioflow/.env` to know which path to check and where the sentinel is.
- **`launcher/src/types.ts`** and **`launcher/src/commands.ts`** — the
  `ShareStatus` type and its invoke wrapper, matching the existing shape of the
  other commands in those files.
- **`launcher/src/Settings.tsx`** — a read-only status row for now. Placed
  directly under the network-exposure checkbox (`:183-202`), because the two
  controls are the same kind of decision and should be read together.
- **Tests, `share.rs` `#[cfg(test)]`:** the `derive_state` table from the spec's
  Testing section. Include a `sharing -l -f json` fixture captured from a real
  machine — a hand-written fixture that does not match Apple's actual JSON
  makes every test in this series pass against a parser that cannot parse.

### 2. `feat(launcher): generate and hand off the share credential`

Backend + credential, still with no system changes. Separable, and it is the
half that can be tested with `pytest`.

- **`backend/app/api/v1/system.py`** (or wherever the system router lives —
  confirm before writing; if there is no obvious home, a new
  `backend/app/api/v1/share.py` router registered alongside `nodes`):
  - `POST /share-credential` — accepts `{username, password}`, refuses any
    `request.client.host` that is not loopback (SS8). The loopback check follows
    `nodes.py:234-237`, which already normalizes `::1` to `127.0.0.1` — reuse
    that normalization or the IPv6 case refuses valid local calls.
  - `DELETE /share-credential`.
  - `GET /share-credential` returns `{configured: bool, username: str | null}`
    and **never the password** (SS7).
- **Storage.** Encrypt with `app.services.ai.crypto.encrypt`, exactly as
  `nodes.py:843` does for `ssh_key_enc`. It needs a home: check whether a
  singleton settings document already exists in `backend/app/models/` before
  adding a new one. If a new model, it needs its Beanie registration — the same
  registration list `Node` and `NodeProvisionTask` appear in. **A model that is
  not registered fails at first query, not at import.**
- **`launcher/src-tauri/src/share.rs`** — `generate_credential()`. `sha2` is
  already a dependency but is a hash, not a CSPRNG; this needs `getrandom` or
  `rand`. Adding a dependency to `launcher/src-tauri/Cargo.toml:18-27` is a real
  change to a deliberately short list — say so in the commit body.
  **Do not** derive the password from anything predictable.
- **`launcher/src-tauri/src/commands.rs`** — the `ureq` POST to
  `http://127.0.0.1:{port}/api/v1/share-credential`, following
  `optional_tools.rs:106-107` for the agent/timeout shape. Literal `127.0.0.1`,
  never the `network_exposed` bind address (SS8).
- **Tests:** `pytest` for the endpoints (round-trip through Fernet; GET omits
  the password; a non-loopback client host is refused; `::1` is accepted).
  Rust-side, assert the log-line builder redacts the credential (SS3).

### 3. `feat(launcher): explain what sharing exposes before turning it on`

The UI, still inert — the toggle exists and the confirmation dialog appears, but
`enable` is not wired. Separable so the wording can be reviewed on its own.

- **`launcher/src/Settings.tsx`** — the checkbox, copied structurally from
  `:183-202`: unchecked default, label framing exposure as the thing being
  turned on, `checkbox-hint` with `role="note"` shown only when on. Wording per
  spec Q5, including the `bioflow-share` account disclosure (SH3).
- The confirmation dialog naming the absolute path.
- **`launcher/src/settings-logic.ts`** — the enable/disable decision as a pure
  function, with cases in `settings-logic.test.ts`. That module already exists
  for exactly this and keeps testable logic out of the component.
- **`launcher/src/launcher.css`** — a style for the third state (SH10) if the
  existing `checkbox-hint` does not carry it.

### 4. `feat(launcher): share the data folder over SMB with one authorization prompt`

The privileged half. Everything under it is now tested.

- **`launcher/src-tauri/src/share.rs`** — `MacosShare::enable`/`disable`, and:
  - `fn build_enable_script(path: &Path, username: &str) -> String` — the whole
    privileged sequence as **one** script: sentinel precondition, `sharing -l`
    inspection, `sharing -a` or `sharing -e`, `-g 000`, `-E 1`, the `dscl`
    account creation, the password read **from stdin**, `launchctl`. Pure
    function returning a `String`, so the one-prompt requirement (SH4) and the
    quoting requirement (SS10) are both unit-testable.
  - `fn shell_quote(s: &str) -> String` and the AppleScript-literal escape on
    top of it. **Two layers.** Its own test, with the adversarial path from the
    spec.
  - The `osascript` invocation, credential on stdin, `-128` mapped to a distinct
    `ShareError::Cancelled` so SH6 does not read as a failure.
- **`launcher/src-tauri/src/lib.rs:16-46`** — register `share_enable` and
  `share_disable`. Same registry as commit 1.
- **`launcher/src-tauri/src/commands.rs`** — the two commands. `enable` asks the
  backend whether a credential is stored **before** running the privileged
  script (Q7 step 4 — the ordering is the rotation guard, and getting it
  backwards means the script sets a password the backend never learns).
  `disable` deletes the stored credential after the script succeeds.
- **`launcher/src/Settings.tsx`** — wire the toggle to the commands.
- **Tests:** the idempotency decision table, the one-`osascript`-invocation
  count, and the quoting test, all per the spec.

### 5. `docs(ops): record what enabling sharing changes on the primary`

Not optional, and it must land with commit 4 or the account creation is
undocumented behaviour.

- **`README.md`** — a shared-storage section, or a pointer from wherever node
  setup is described. `README.md` currently mentions nodes only at `:126` (a
  database note), so this needs a real home rather than an insertion point.
- **`docs/superpowers/specs/2026-08-10-multi-node-design.md`** — its deferred
  "Phase 2 cross-node data transfer" is what this epic builds. Check whether it
  now says something false; if so it changes in this commit or it starts lying.
- The `bioflow-share` account, by name, and how to remove it by hand.

### 6. `test(launcher): cover the share state machine end to end`

Only if commits 1-4 left gaps. Prefer folding tests into their own commits; this
is a placeholder to delete, not a target.

### 7. `chore(launcher): note the share credential in the release brief`

Check `docs/RELEASE_BRIEF.md` and `docs/TODO.md` for entries this closes. Per
CLAUDE.md, a resolved `docs/TODO.md` entry gets ` — FIXED` and moves to
`docs/TODO-done.md` in this commit or the next.

### 8-9 (only if Q1's split is rejected)

`feat(launcher): share the data folder over Samba on Linux primaries`, plus its
docs commit. A `LinuxShare` impl behind the same trait: `testparm -s` and
`systemctl is-active` for `state()`; an `/etc/samba/smb.conf` stanza and
`smbpasswd -a` for `enable()`; `pkexec` for elevation with a **refusal**, not a
`sudo` fallback, when no polkit policy is installed. The distro service-name
matrix (`smbd` vs `smb`) and the "samba is not installed" case are most of the
work.

## What else changes, or starts lying

- **`launcher/src-tauri/src/lib.rs:16-46`** — three new commands. Unregistered,
  they fail only at the frontend invoke, with no compile error.
- **`launcher/src-tauri/Cargo.toml:18-27`** — a CSPRNG dependency, on a
  deliberately minimal list.
- **`launcher/src-tauri/src/settings.rs:97-100`** — `render_env` replaces `.env`
  wholesale. Confirm nothing in this series writes share state there; if
  anything does, it is dropped on the next settings change (the exact silent
  regression the `env_extra` parameter at `:88` exists to prevent).
- **`backend/app/api/v1/nodes.py`** — untouched here, but #848 will read the
  credential this stores. The two must agree on the key name; #848's spec should
  cite this one.
- **`docs/superpowers/specs/2026-08-10-multi-node-design.md`** — commit 5.

## Verification

- `cd launcher/src-tauri && cargo test` — the Rust unit tests.
- `cd launcher && npm test` — `settings-logic.test.ts`.
- `./backend/run-worktree-tests.sh tests/ -q` from the worktree, per CLAUDE.md.
  **Not `docker compose exec api`** — from a worktree that tests main's code.
- `ruff check --config backend/pyproject.toml backend/app backend/tests ops e2e`
  from the repo root.
- **On the real macOS primary, and nothing else proves these:**
  - Exactly one authorization dialog on enable.
  - `sharing -l` reports `guest access: 0` for `bioflow`.
  - A second machine mounts with the credential and is **refused with a wrong
    one**. An "it mounted" check passes on a guest-accessible share.
  - Enable twice: one share record, and the stored credential is byte-identical
    before and after.
  - Disable: `sharing -l` clean, `dscl . -list /Users` has no `bioflow-share`,
    `GET /share-credential` reports `configured: false`.
  - Unplug the external volume, poll: the UI shows the third state, not green.
- **The #844 probe green against a real node**, which is the actual acceptance
  criterion for the epic and the only thing that proves the share is usable.

## Out of scope

- #848 (mount on the node), #849 (the `ops/` script), #850 (teardown).
- Linux primaries, if Q1's split is accepted.
- Credential rotation.
- Authenticating the BioFlow API.
