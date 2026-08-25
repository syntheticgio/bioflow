# Configure the SMB share on the primary from the launcher — design

Date: 2026-08-25.

Addresses [#847](https://github.com/syntheticgio/bioflow/issues/847). Child 4 of
[#843](https://github.com/syntheticgio/bioflow/issues/843).

**Depends on [#844](https://github.com/syntheticgio/bioflow/issues/844)** — the
probe is the only thing that proves the share works; without it this ticket
ships a share nobody has verified.

**This spec recommends splitting #847 in two** (Decision Q1) and flags one
decision the user must make before implementation starts (Decision Q4).

## What exists today

Verified against this worktree on 2026-08-25.

### The launcher has never elevated, and has no secret store

Two facts that together define most of this ticket's cost:

- **No elevation anywhere.** The only occurrence of `sudo` in
  `launcher/src-tauri/` is a string in an error hint —
  `launcher/src-tauri/src/commands.rs:1229` ("Linux: sudo apt install sshpass").
  Nothing in the launcher runs a privileged process, requests an authorization
  right, or shells out to `osascript`/`pkexec`. There is no precedent to copy.
- **No secret store.** `launcher/src-tauri/Cargo.toml:18-27` lists the whole
  dependency set: `serde`, `serde_json`, `log`, `tauri`, `tauri-plugin-log`,
  `tauri-plugin-opener`, `dirs`, `ureq`, `sha2`, `libc`. No `keyring`, no
  `security-framework`, no `tauri-plugin-stronghold`. The launcher's persistent
  state is `~/.bioflow/.env`, a plaintext file
  (`launcher/src-tauri/src/settings.rs:97-100` writes it whole, and
  `commands.rs:70-72` fixes the directory at `~/.bioflow`). SSH passwords are
  held in a `String` for one call and dropped
  (`launcher/src-tauri/src/remote.rs:27` `password: Option<String>`); nothing is
  ever written.

`LauncherApp` (`commands.rs:55-60`) holds three `Mutex`es — `install_dir`,
`port`, `migration_progress`. No credential field.

### The launcher already calls the backend over HTTP

This is the handoff mechanism, and it already exists:

- `launcher/src-tauri/src/optional_tools.rs:106-107` —
  `ureq::AgentBuilder` against `http://localhost:{port}/api/v1/pipelines/tools`.
- `launcher/src-tauri/src/commands.rs:1091-1096` — `ureq::get` against
  `http://{host}:{port}/api/v1/nodes/connection-details`.

The endpoint at `backend/app/api/v1/nodes.py:225-254` takes no authentication.
**Nothing in `backend/app/api/v1/nodes.py` uses `Depends`, an API key, or any
auth at all.** The API is unauthenticated by design; `network_exposed` in the
launcher's own Settings UI says so plainly:
"Anyone on your network can reach BioFlow with no login required."
(`launcher/src/Settings.tsx:198-201`.)

### The backend's existing secret-at-rest mechanism

`backend/app/services/ai/crypto.py` — Fernet, key at
`$BIOINFO_HOME/.biopipe/secret.key`, written `0600` at creation
(`crypto.py:47-49`). Its module docstring states the honest scope:

> the key file is on the same disk as the Mongo data, so anyone with shell
> access to this machine has both and can decrypt everything. What it defends
> against is a look at the collection — an opened Compass window, a stray
> `mongodump` in a backup.

`Node.ssh_key_enc` (`backend/app/models/node.py:42`) uses it via
`nodes.py:843` (`crypto.encrypt(private_pem)`) and
`node_update_service.py:87` (`crypto.decrypt`).

### macOS: `sharing`, `smbd`, and the guest-access default

Verified on the maintainer's own machine (Darwin 25.6.0):

- `/usr/sbin/sharing` exists, `root:wheel`, mode `0755`. `man 8 sharing`
  confirms `-a <path>`, `-S <smb name>`, `-s <flags>` (`001` = enable SMB),
  `-r <name>` (delete), `-l` (list), and two options the issue does not mention
  and this spec requires: **`-g <flag>`** and **`-E <0/1>`**.
- **`-g` defaults to guest access ON.** The man page: "By default guest access
  is enabled for smb." Confirmed empirically — `sharing -l` on this machine
  reports `guest access: 1` for both existing Public Folder shares. The issue's
  reference command `sharing -a <path> -S bioflow -s 001` would therefore
  create a **guest-accessible share**, directly violating its own acceptance
  criterion. `-g 000` is mandatory.
- **`-E 1`** enables SMB3 encryption for the share. Not required by the issue;
  cheap, and the traffic is bulk genomic data over a LAN.
- `sharing -l` reads the live share-point records — this is the answer to Q6 on
  macOS, and its `-f json` option makes it parseable.
- `/System/Library/LaunchDaemons/com.apple.smbd.plist` exists and
  `plutil -p` reports **`"Disabled" => true`** on a stock machine. So the
  daemon genuinely does need enabling; `sharing -a` alone exports nothing.
  `man launchctl` lists `load -w` under **LEGACY SUBCOMMANDS** with
  "Recommended alternative subcommands: bootstrap | bootout | enable | disable".

### BIOINFO_HOME on this deployment

`docker-compose.yml:23` — `BIOINFO_HOME_HOST: ${BIOINFO_HOME:-/Volumes/FastDataExtension/BioinfoHelper}`.
The maintainer's primary is macOS with BIOINFO_HOME on an external volume.

`backend/app/storage/home.py:1-11` documents why that matters:

> On macOS, Docker Desktop presents an unmounted bind-mount source as an
> *empty directory*, not an error.

`settings.sentinel_path` (`backend/app/config.py:632-634`) =
`$BIOINFO_HOME/.biopipe/VERSION`; `lock_path` (`:637-638`) =
`.biopipe/lock`, taken with `fcntl` (`storage/home.py:15`).

## Decision Q1: split #847 into a macOS ticket and a Linux ticket

**Recommendation: split. This is a decision for the user to confirm.**

The issue invited this ("split this ticket if that proves true in the plan").
It proves true. The two platforms share the *shape* of the feature and almost
none of its substance:

| | macOS | Linux |
|---|---|---|
| Declare the share | `sharing -a` (stock binary, opaque record store) | edit `/etc/samba/smb.conf` (a text file this repo would own a stanza inside) |
| Start the service | `launchctl` on a system LaunchDaemon | `systemctl enable --now smbd` — and there is no one service name (`smbd` on Debian/Ubuntu, `smb` on Fedora/RHEL) |
| Install the server | never — it ships | `samba` is **not installed by default** on most distributions; this is an `apt`/`dnf` install with no portable command |
| Read real state | `sharing -l -f json` | parse `smb.conf` + `systemctl is-active` + `testparm` |
| Credential store | Open Directory, via `dscl` / `sysadminctl` | `smbpasswd -a`, a separate database from `/etc/shadow` |
| Elevation | `SMJobBless`, or `osascript … with administrator privileges` | `pkexec` (needs a policy file) or `sudo` in a terminal |

Every row is a different implementation. There is no useful shared abstraction
below "enable sharing / disable sharing / report state" — three function
signatures, which is not enough shared code to justify carrying both platforms
through one review, one test matrix, and one merge.

**The seam:** a Rust trait in a new `launcher/src-tauri/src/share.rs`:

```rust
pub trait SharePlatform {
    fn state(&self) -> ShareState;             // Q6, live query
    fn enable(&self, path: &Path, cred: &ShareCredential) -> Result<(), ShareError>;
    fn disable(&self) -> Result<(), ShareError>;
}
```

plus the credential generation, the handoff to the backend (Q3), and the UI —
all platform-independent, and all of it needed by whichever half lands first.

- **#847a (this spec's primary target): the seam, the credential, the UI, and
  the macOS implementation.** macOS first because it is the maintainer's actual
  primary (Q8), because the tooling is stock, and because it is the platform
  where "the launcher can do this for you" has the most value — a Linux user
  running `docker compose` directly is already the #849 audience.
- **#847b: the Linux implementation** behind the same trait. Its extra cost is
  the distro matrix, not the SMB.

If the user prefers one ticket, the plan still lands macOS first and Linux
second as separate commits; the split is about review and merge granularity,
not about ordering.

## Decision Q2: `osascript … with administrator privileges`, one call, for the whole sequence

**macOS.** The correct-by-Apple's-lights mechanism is a privileged helper tool
installed with `SMJobBless` and talking XPC. It is also a signed-helper
plist-matching build-system change, a second binary in the bundle, and a class
of code-signing failure this project has already spent effort on
(`docs/macos-signing.md`). For a single-user local tool that shells out to
`docker compose` for everything else, it is the wrong size.

Use instead:

```
osascript -e 'do shell script "<script>" with administrator privileges \
  with prompt "BioFlow needs administrator access to share your data folder over your local network."'
```

This raises the OS's own authentication dialog (Touch ID / password), returns
the script's stdout, and returns a distinguishable error (`-128`) when the user
cancels. **The `with prompt` string is the epic's "explaining what it
authorizes" requirement** and must name the folder.

**Exactly one prompt** therefore means: the entire privileged sequence —
`sharing -a`, the guest-access and encryption flags, the credential
provisioning of Q4, and `launchctl` — is **one shell script passed to one
`osascript` call.** Not four calls. Getting this wrong is easy and only shows
up as four dialogs on a real machine, which no unit test catches.

Residual risk, stated plainly:

- **`do shell script` composes a shell command line from strings.** The share
  path is user-chosen and on this deployment contains no metacharacters, but
  it can. Every interpolated value must be single-quoted with embedded quotes
  escaped, and the *AppleScript* string literal escaped on top of that — two
  layers, and the outer one is easy to forget. This needs its own unit test
  with a path containing `'`, `"`, `$`, `;` and a space.
- **The credential must not appear in the command line**, where it is visible
  in `ps` to every user on the machine for the life of the call. It is passed
  on the privileged script's **stdin**, and the script reads it into a shell
  variable.

**Linux (#847b).** `pkexec` is the equivalent, and it requires shipping a
polkit `.policy` file into `/usr/share/polkit-1/actions/` — which itself needs
root to install, so it belongs to the packaging step, not to runtime. Where no
policy file is present, the launcher must **not** silently fall back to a
terminal `sudo`; it should refuse with the `ops/` script (#849) as the remedy.
That refusal is the honest answer and is why #847b is the lower-value half.

## Decision Q3: the credential goes to the backend over the existing HTTP call, encrypted with the existing Fernet

The crux. The launcher (Rust process) generates the credential; node
provisioning (`backend/app/api/v1/nodes.py:_provision_node`, `:671`, in the
`api` container) needs it, potentially days later.

**Rejected: the launcher keeps it.** Nothing in the backend can reach a Tauri
process — the launcher may not even be running when a node is provisioned. And
building a secret store in the launcher means adding `keyring`/
`security-framework`, which is a new dependency, a new Keychain-prompt failure
mode, and a second place secrets live.

**Rejected: `~/.bioflow/.env`.** It is plaintext, it is rewritten whole by
`settings.rs:97-100` on every settings change, and it is mounted into
containers. A credential there is a credential in `docker inspect` — exactly
what `crypto.py`'s docstring says it moved *away* from.

**Chosen: the backend stores it, the launcher hands it over once.**

1. The launcher generates the credential locally (Q7 decides when).
2. It `POST`s to a new `/api/v1/system/share-credential` on the primary's own
   API — the same `ureq`-to-localhost call it already makes at
   `optional_tools.rs:106-107`, **bound to `127.0.0.1` only**, never the
   `network_exposed` address.
3. The backend encrypts with `app.services.ai.crypto.encrypt` and stores it in
   a settings document. Same Fernet, same key file, same honest threat model
   as `ssh_key_enc`.
4. `_provision_node` decrypts it at mount time (#848) and passes it to the node
   in the credentials file, never on a command line.

**Yes, `crypto.py` is the right home**, and its docstring is the reason: it
already states exactly what this protects against (a look at the collection)
and what it does not (shell access to the machine). Adding a second, differently
scoped secret store would mean two threat models to explain instead of one.

Residual risks, all of which the UI must not overstate away:

- **The POST is unauthenticated**, because nothing in this API is
  (`nodes.py` has no `Depends`). Localhost-only binding is the whole control.
  Anything running as any user on the primary can set the stored credential.
  On a single-user local tool that is the same posture as everything else here,
  but it should be written down rather than discovered.
- The credential is recoverable by anyone with shell access to the primary.
  That is `crypto.py`'s stated scope, not a new weakness.

The credential is **never returned by any GET**. `/share-credential` accepts
`POST` and `DELETE`; the status endpoint reports only whether one is set.

## Decision Q4: yes — a dedicated system account is required, and it is the biggest cost in this ticket

**This is the decision that most needs the user's explicit sign-off.**

There is no way around it. SMB authentication on both platforms binds to a
system account database:

- **macOS.** `smbd` authenticates against Open Directory. A share created with
  `sharing -a … -g 000` is reachable only by a local user account. There is no
  "share-only user" that `sharing` itself can create. Creating one means
  `sysadminctl -addUser` or a `dscl . -create` sequence, plus enabling the SMB
  auth authority for that user.
- **Linux.** `smbpasswd -a <user>` requires `<user>` to already exist in
  `/etc/passwd`. Samba's password database is separate from `/etc/shadow`, but
  it does not create the Unix account.

So "share a folder" is, honestly, **"create a system account and share a folder
to it."** That is a materially larger thing than the issue title suggests, and
pretending otherwise would produce an implementation that silently creates an
account nobody agreed to.

**Recommended: create a dedicated, hardened, non-login account** named
`bioflow-share`, and disclose it in the enable dialog by name.

- Shell `/usr/bin/false`, no home directory, hidden from the macOS login window
  (`dscl … create … IsHidden 1`), a UID in the system range, and a member of no
  admin group.
- Its only purpose is SMB auth to one share.
- Disabling sharing (R7) **deletes the account**, not merely the share.

**Rejected: reuse the user's own account.** It works with zero account
creation, and it is worse in every other way: the credential the launcher
generates would have to *be* the user's login password (which the launcher must
never learn or store), or the account's password would have to be *changed* to
a generated value — locking the user out of their own machine. Not viable.

**Rejected: guest access.** Removes the account problem entirely and is
forbidden by the issue's own acceptance criteria ("not guest-accessible"), for
good reason: it exports genomic data to every device on the LAN.

**The honest alternative, if the user rejects account creation:** #847 becomes
detection-and-instruction rather than automation — the launcher checks whether a
share exists and, if not, shows the exact commands and points at #849. That is a
much smaller ticket and a defensible one. It is the fallback to choose if
"the launcher creates a system user" is not acceptable.

## Decision Q5: an explicit, disclosed toggle in Settings, off by default, modelled on the network-exposure control

`launcher/src/Settings.tsx:183-202` is the existing idiom and this follows it
exactly: an unchecked checkbox whose label is framed as turning exposure *on*
(never as turning safety on), with a `checkbox-hint` that appears only when
checked and states the consequence in plain language.

- **Label:** "Share my data folder with compute nodes over the network"
- **Hint, shown when on:** "Any device on your network that has the share
  password can read and write your BioFlow data folder. BioFlow creates a
  dedicated `bioflow-share` account for this and removes it when you turn
  sharing off."
- **The off switch is the same checkbox**, in the same place. Not a separate
  destructive-action screen — an on/off control that is hard to find when off is
  a control users do not trust.
- Checking it opens a confirmation dialog naming **the exact path** being shared
  and the account being created, then raises the single `osascript` prompt
  (Q2). Cancelling the OS prompt leaves the checkbox unchecked.
- **The credential is never displayed.** The status row shows "Sharing is on"
  and the share name; there is no reveal control, because the only consumer is
  node provisioning, which reads it from the backend.

`settings-logic.ts` has an established pure-logic + `.test.ts` pattern
(`settings-logic.test.ts`); the enable/disable decision logic belongs there.

## Decision Q6: query the system every poll; never render a stored flag

The epic's requirement, and the one most likely to be quietly violated by
caching the result of a successful enable.

**macOS** — two independent facts, both live, both required:

1. `sharing -l -f json` — is there a share-point record named `bioflow`, and
   does it report `shared: 1`, `guest access: 0`?
2. `launchctl print system/com.apple.smbd` — is the daemon actually loaded?
   (`plutil -p` on the plist confirms stock state is `Disabled => true`, so this
   is a real failure mode, not a theoretical one.)

Both must be true for the UI to say "on". A share record with a dead daemon is
the exact state a user would otherwise see as green and a node would see as
unreachable.

**Linux (#847b)** — `testparm -s` for the `[bioflow]` stanza, plus
`systemctl is-active` on the distro's service name.

Neither query needs elevation, so the poll is free of prompts. This is checked
on the same cadence as the existing status poll, and **a share that vanished
because someone ran `sharing -r bioflow` by hand must show as off on the very
next poll** — the same discipline `state.rs:41-45` already documents for the
Docker daemon ("never cached across calls").

## Decision Q7: enable is idempotent by inspection, and never rotates an existing credential

Enabling twice must be a no-op. The sequence, in the privileged script:

1. If `sharing -l` already reports a `bioflow` record for **this same path**,
   do not call `sharing -a`. Call `sharing -e bioflow …` to reconcile the flags
   (`-g 000`, `-E 1`) instead — which fixes a hand-edited share rather than
   duplicating it.
2. If the record exists for a **different** path, that is a conflict, not a
   no-op: fail with the existing path named and the remedy ("turn sharing off
   first"). Silently repointing a share is how a node ends up reading the wrong
   directory.
3. If the account exists, do not recreate it and **do not touch its password**.
4. Generate a credential **only when the backend reports none is stored.** This
   is the rotation guard, and the ordering matters: the backend is asked first,
   before the privileged script runs, because the launcher cannot un-rotate a
   password it has already set.
5. `launchctl` is idempotent by nature; enabling a loaded daemon is a no-op.

**Deliberately no rotation feature.** Rotating means every mounted node has a
stale credential and fails at the next mount, and nothing in this system
re-mounts nodes. Off-then-on is the supported path, and it goes through #850's
teardown.

## Decision Q8: the external-volume case is the deployment, and it needs the sentinel and a remount guard

BIOINFO_HOME here is `/Volumes/FastDataExtension/BioinfoHelper`
(`docker-compose.yml:23`). Three specific consequences:

1. **Do not enable sharing against an unmounted volume.** `storage/home.py:1-11`
   is explicit that macOS presents an unmounted bind-mount source as an empty
   directory. `sharing -a` on that path would succeed and export an empty
   directory — a green share and a node that finds nothing. **Precondition:
   `settings.sentinel_path` (`$BIOINFO_HOME/.biopipe/VERSION`,
   `config.py:632-634`) must exist before the privileged script runs.** Reuse
   the existing sentinel; do not invent a second mount check.

2. **The share does not survive a volume that fails to remount.** macOS
   share-point records are path-based and persist across reboots; the *volume*
   may not. After a reboot with the drive unplugged, `sharing -l` reports the
   share as present while the path is empty. So the status query of Q6 must
   **also** check the sentinel, and report a distinct third state — "Sharing is
   on, but your data folder is not mounted" — rather than a green "on". This is
   the same tri-state shape `SettingsNodes.tsx:515-521` already uses for node
   health (Online / Offline / Unknown with an explanatory `title`).

3. **POSIX locking over SMB.** `settings.lock_path` is taken with `fcntl`
   (`storage/home.py:15`, `.biopipe/lock`). `fcntl` locks over SMB are
   advertised by both macOS and Linux CIFS clients but are a documented source
   of silent misbehaviour, especially with `nobrl`-style mount options. **In
   scope for this ticket: nothing** — the primary takes the lock on a local
   filesystem, unchanged. **In scope for #848 and #844's probe:** whether a
   *node* holding `.biopipe/lock` over CIFS actually excludes the primary. This
   spec flags it because it is the failure that would look like data
   corruption rather than a mount error, and it must be resolved before nodes
   write. Listed under "Verify before implementing".

## Requirements

Permanent IDs; never reused.

- **SH1.** A user can turn on sharing of `BIOINFO_HOME` from the launcher's
  Settings screen.
- **SH2.** Sharing is off in a fresh install and after an upgrade from a version
  without this feature.
- **SH3.** Before any system change, the launcher shows the user the absolute
  path that will be shared and the name of the account that will be created.
- **SH4.** Turning sharing on raises exactly one OS authorization prompt.
- **SH5.** The authorization prompt's text names the action being authorized.
- **SH6.** Cancelling the authorization prompt leaves the system unchanged and
  the toggle off.
- **SH7.** A user can turn sharing off from the same control that turned it on.
- **SH8.** Turning sharing off removes the `bioflow` share point, stops
  advertising it, deletes the `bioflow-share` account, and deletes the stored
  credential.
- **SH9.** The launcher's reported sharing state is derived from a live query of
  the operating system on each status poll.
- **SH10.** When the share exists but `BIOINFO_HOME`'s sentinel is absent, the
  launcher reports a state distinct from both "on" and "off".
- **SH11.** Turning sharing on when a `bioflow` share already exists for the
  same path does not create a second share point.
- **SH12.** Turning sharing on when a credential is already stored does not
  generate a new one.
- **SH13.** Turning sharing on when a `bioflow` share exists for a *different*
  path fails, naming the existing path, without modifying it.
- **SH14.** The launcher refuses to enable sharing when
  `$BIOINFO_HOME/.biopipe/VERSION` does not exist.
- **SH15.** Node provisioning can retrieve the credential from the backend
  without the launcher running.

### Security requirements

- **SS1.** The `bioflow` share denies guest access.
- **SS2.** The share's credential is generated by a cryptographically secure
  random source and is at least 20 characters.
- **SS3.** The credential is never written to the launcher's log, the backend's
  log, `~/.bioflow/.env`, or any file the launcher writes in plaintext.
- **SS4.** The credential is never displayed in the launcher UI, in any form,
  including masked.
- **SS5.** The credential is never passed as a command-line argument to any
  process.
- **SS6.** The credential is stored encrypted at rest, using the existing
  `app.services.ai.crypto` Fernet key.
- **SS7.** No API endpoint returns the credential in plaintext to any caller
  other than the node-provisioning path within the backend process.
- **SS8.** The endpoint that accepts the credential from the launcher is
  reachable only from `127.0.0.1`.
- **SS9.** The `bioflow-share` account cannot log in interactively and is a
  member of no administrator group.
- **SS10.** Every value interpolated into the privileged shell script is quoted
  such that a path containing `'`, `"`, `$`, `;`, or a space cannot alter the
  commands executed.

## Testing

The launcher's established pattern is pure-logic modules with `.test.ts`
(`launcher/src/settings-logic.test.ts`) and `#[cfg(test)]` Rust tests with a
fake backend (`state.rs`'s `FakeDocker`). Both apply.

- **SH9/SH10, `ShareState` derivation** — a pure function over
  `(sharing -l output, launchctl output, sentinel exists)`. Table-driven: share
  present + daemon up + sentinel = On; share present + daemon down = Off with a
  reason; share present + no sentinel = the third state; no share = Off. This is
  the highest-value test in the ticket and needs no root.
- **SH11/SH12/SH13, the idempotency decision** — a pure function over the same
  parsed state plus "is a credential stored", returning
  `NoOp | Reconcile | Create | Conflict{existing_path}`. Assert `Create` is
  returned exactly once across two consecutive enables.
- **SS10, quoting** — feed `/Volumes/My Drive/it's "here"; rm -rf /` through the
  script builder and assert the produced AppleScript, when parsed, yields one
  argument. A test that only checks "contains a quote" passes on a broken
  builder.
- **SH4, one prompt** — assert the builder produces exactly one `osascript`
  invocation for a full enable, by counting invocations against a fake command
  runner. A real machine is the only place four dialogs are visible, which is
  precisely why this needs a unit test.
- **SS3/SS4/SS5** — assert the credential does not appear in the rendered
  command line, and that the log-line builder redacts it. Cheap, and these are
  the requirements most likely to regress in a later refactor.
- **SS6/SS8, backend** — `pytest` per CLAUDE.md, via
  `./backend/run-worktree-tests.sh`. Assert the stored value round-trips through
  `crypto.encrypt`/`decrypt`, that no GET returns it, and that a request with a
  non-loopback client host is refused.
- **Real-machine check, not automatable:** on the maintainer's macOS primary,
  enable, confirm one dialog, `sharing -l` shows `guest access: 0`, mount from a
  second machine with the credential and confirm the wrong credential is
  refused, then disable and confirm `sharing -l` and `dscl . -list /Users` are
  both clean.

## Verify before implementing

1. **Whether `sharing -a` on a `/Volumes/...` path behaves identically to a path
   under `/Users`.** External volumes have their own ACL and ignore-ownership
   semantics; a share that exports the right path with the wrong effective
   permissions is the deployment's most likely failure.
2. **The exact `dscl`/`sysadminctl` sequence for a hidden, non-login,
   SMB-authenticating account on Darwin 25**, and whether it needs the account
   to be enabled for SMB explicitly. This is Q4's cost and it should be measured
   before the ticket is accepted, not during it.
3. **Whether `osascript … with administrator privileges` reads stdin
   reliably** for the credential handoff (Q2), and what it returns on cancel.
4. **Whether `launchctl enable system/com.apple.smbd` plus `kickstart` is
   sufficient**, or whether the legacy `load -w` is still the only thing that
   works — `man launchctl` marks `load` legacy but does not say it fails.
5. **`fcntl` locking over CIFS from a Linux node against a macOS `smbd`** (Q8.3).
   Blocking for #848, not for this ticket, but the answer changes what #848 can
   assume.
6. **Whether the maintainer accepts the `bioflow-share` account (Q4).** If not,
   this spec's fallback replaces most of it.

## Out of scope

- **Mounting on the node.** #848.
- **Tearing down the mount when a node is removed.** #850.
- **The probe.** #844; this ticket depends on it and does not reimplement it.
- **The non-launcher path.** #849, specified alongside this.
- **Linux primaries**, if Q1's split is accepted — #847b.
- **macOS compute nodes.** Out of scope for the whole epic (#843).
- **Credential rotation** (Q7). Off-then-on is the supported path.
- **Authenticating the BioFlow API.** The API is unauthenticated by design;
  changing that is a much larger decision than this ticket.
- **A privileged helper tool via `SMJobBless`** (Q2). Revisit only if
  `osascript` proves unusable.
