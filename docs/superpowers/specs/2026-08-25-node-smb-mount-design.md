# Mount the primary's SMB share on a Linux node during provisioning — design

Date: 2026-08-25.

Closes [#848](https://github.com/syntheticgio/bioflow/issues/848). Child 5 of
[#843](https://github.com/syntheticgio/bioflow/issues/843).

**Depends on [#844](https://github.com/syntheticgio/bioflow/issues/844)** — the
round-trip sentinel probe is the only thing that can tell a correct mount from
a plausible one. **Depends on [#847](https://github.com/syntheticgio/bioflow/issues/847)**
— there is no share to mount until the primary exports one.

## The central problem: this ticket adds sudo to provisioning

Everything below is downstream of one fact. **There is no sudo anywhere in
`backend/` or `ops/`** (GROUND.md §A; the only repo-wide occurrence is an error
hint in `launcher/src-tauri/src/commands.rs:1229`). Provisioning runs end to end
as an unprivileged SSH user, and where root is genuinely required the
established posture is to *refuse*: `verify_docker` (`nodes.py:768-777`) does
not install Docker, it checks for it and fails with "Install Docker first."

`cifs-utils` installation, `/etc/fstab`, a root-owned credentials file and
`mount(8)` all require root. So this feature cannot be built inside the existing
posture. The user has decided to add sudo rather than extend the refuse-and-
instruct posture to mounting.

That decision is not a detail of this ticket. It creates a privileged channel
into every provisioned node that all *future* provisioning work will be able to
reach for, and it does so with no precedent in this codebase to copy. The
closest thing — `node_ssh.py`'s Fernet-encrypted managed key and TOFU host-key
pinning — is a *credential-minimisation* precedent, not an escalation one: its
whole design is that the user's credential is used once and never stored
(`node_ssh.py:1-12`). The decisions below are written to stay inside that
philosophy rather than to reverse it.

**Residual risk, stated plainly and not softened:** after this ships, a BioFlow
primary that is compromised can run arbitrary root commands on every Linux node
it provisions, for the duration of a provisioning run. The mitigations below
reduce the *window* and the *blast radius*; none of them removes that fact.
`crypto.py:7-11` sets the honesty standard for this project — "the honest scope
of this" — and this spec follows it.

## What exists today

Verified against this worktree on 2026-08-25.

- **`_provision_node`** (`backend/app/api/v1/nodes.py:671`) runs nine phases:
  `validate_ssh` (:705), `write_env` (:750, failure-only), `verify_docker`
  (:768), `setup_install` (:779, :789), `write_env` (:802), `install_key`
  (:828), `pull_image` (:861), `start_worker` (:873), `verify` (:892),
  `enrolled` (:899).
- **`RemoteStep`** (`nodes.py:483-500`) carries `phase, message, command,
  timeout, describe_failure`. `_execute_remote_commands` (:539-556) runs each
  via `conn.run(step.command, check=False)` with `asyncio.wait_for`. **There is
  no stdin path** — the command is a shell string and nothing else.
- **`on_progress` is awaited once per *phase*, not per command** (:545-549). The
  docstring (:541-543) states a phase string "is a user-visible contract, not an
  internal label."
- **`ProvisionRequest`** (`nodes.py:40-56`) has `host, port, username,
  password | private_key` (exactly one, validated :50-56), `node_name`,
  `storage_location="/data/scratch"`, `worker_replicas`. `storage_location` has
  no validator of any kind.
- **`_render_node_env`** (:459-478) writes `storage_location` verbatim into
  `BIOINFO_HOME` and `BIOINFO_REGISTER_ROOTS`. No quoting, no escaping.
- **`_render_node_compose`** (:406-456) gives `BIOINFO_HOME` three distinct
  roles: `.env` host path, container path `/data` (:434), and
  `BIOINFO_HOME_HOST: ${BIOINFO_HOME}` (:436) re-injected **for sibling
  containers started through the mounted Docker socket**. The bind mount is
  `- ${BIOINFO_HOME}:/data` (:443). The two renderers' docstring (:420-422)
  says they "must be changed together."
- **`bioinfo_home_host`** (`config.py:299-305`) exists precisely because "a
  sibling container gets its mounts from the host, so it needs this value and
  not the container's own view." Sole consumer `pipelines/variant_runner.py:213`
  `host_path_for`.
- **`sentinel_path`** (`config.py:632-634`) = `$BIOINFO_HOME/.biopipe/VERSION`,
  docstring "Proves the drive is actually mounted." `SENTINEL_CONTENT =
  "biopipe-home-v1\n"` (`storage/home.py:25`). **This is the existing
  mount-verification mechanism.**
- **`lock_path`** (`config.py:637-638`) = `.biopipe/lock`. Used only by
  `storage/home.py:129` `_acquire_lock`, via `fcntl.flock(LOCK_EX|LOCK_NB)`.
- **`_assert_same_filesystem`** (`storage/home.py:93-108`) requires
  `objects/`, `staging/` and `tmp/` to share a `st_dev`, because blob placement
  finishes with `os.rename()` (`storage/cas.py:144`).
- **`crypto.encrypt/decrypt`** (`services/ai/crypto.py`) is Fernet with the key
  at `$BIOINFO_HOME/.biopipe/secret.key`. `ssh_key_enc` is persisted this way
  (`nodes.py:843`) **because `node_update_service.py:87` needs it later** to
  push updates. That is the test for whether a secret earns persistence.
- **The worker container runs as root.** `backend/Dockerfile` contains no
  `USER` directive; no compose file sets `user:`. Confirmed empirically:
  `docker exec biopipe-api-1 id` → `uid=0(root) gid=0(root)`.
- **`asyncssh` is pinned `>=2.18,<3`** (`backend/pyproject.toml:24`); the
  installed version is **2.24.0**. `SSHClientConnection.run` has signature
  `(self, *args, check=False, timeout=None, **kwargs)` and its docstring states
  "All of the arguments to `create_process` can be passed in to provide input
  or redirect stdin". **`input=` is supported.** Confirmed by inspecting the
  installed library, not from memory.

## Decision Q1: the sudo credential is never persisted

Add `sudo_password: str | None = None` to `ProvisionRequest`. It lives in the
Pydantic model for the life of the `_provision_node` task and is never written
to Mongo, never to a log, never to disk.

**The contrast with `ssh_key_enc` is the whole argument.** `ssh_key_enc` *is*
persisted, under Fernet, and that is correct — `node_update_service.py:87`
decrypts it every time the user updates a node, so the alternative is asking for
a credential on every update. Persistence there buys a recurring capability.

Mounting buys nothing recurring. The fstab entry makes the mount survive reboot
without any further privileged action, so once provisioning completes the node
needs no root from BioFlow again. A secret with no second use has no
justification for a second lifetime.

**Three sources, in preference order, and the node decides which it needs:**

1. **NOPASSWD sudo** — if `sudo -n true` succeeds, no credential is required at
   all and none is requested. This is the best outcome and the spec should say
   so in the UI: a user who configures NOPASSWD for the BioFlow SSH user gives
   BioFlow *less* to hold, not more.
2. **An explicitly supplied `sudo_password`.** A separate field, not silently
   reused.
3. **The SSH password**, when password auth was used, and **only when the user
   ticks a box saying so.** Reusing it implicitly is rejected: the user typed
   that password to answer "who are you", and silently re-purposing it to answer
   "may you become root" is a change of meaning they did not consent to. The tick
   box makes the re-purposing an explicit act.

If none of the three is available, provisioning **refuses with a remedy**,
following the `UnroutablePrimaryHost` shape (`nodes.py:280-281`, :742-751):
what was found → why it is wrong → the exact remedy → "provision the node
again." The remedy names both options: configure NOPASSWD for this user, or
re-provision supplying a sudo password.

### The tension Q1 must resolve: #850 teardown

**#850 (tear down the mount when a node is removed) will need root again**, and
by then the credential is gone. This is real and must not be waved away.

Resolution: **#850 does not get to re-authenticate, and should not want to.**
Three reasons.

- Revocation today does nothing to the remote machine at all — `revoke_node`
  (`nodes.py:1073-1086`) flips `status = "revoked"` and saves; it does not
  `docker compose down`, remove `~/.bioflow`, or strip the authorized_keys line
  (GROUND.md §B). Teardown of the *mount* would be the single most privileged
  thing BioFlow does to a node it is in the act of forgetting.
- Retaining a root credential for months so that a *future* delete can unmount a
  filesystem inverts the risk: the stored secret is dangerous continuously, the
  unmount is convenient once.
- The failure mode of *not* unmounting is benign. A stale CIFS mount to a
  primary that has stopped exporting is inert; with the Q4 options it does not
  hang boot, and the node is no longer running BioFlow work.

So #850's correct shape is: **prompt for a sudo credential at teardown time if
the user wants the mount removed, and otherwise print the two commands
(`umount`, remove the fstab line) for them to run.** That is a decision this
spec makes *for* #850, and #850 should cite it rather than re-open it.

**Requires user approval.** This forecloses an option in a downstream ticket.

## Decision Q2: the password reaches sudo over stdin, never a command line

Use `sudo -S -p '' -- <command>` with the password written to the process's
stdin via asyncssh's `input=` kwarg.

```python
result = await conn.run(
    f"sudo -S -p '' -- {command}",
    check=False,
    input=f"{sudo_password}\n",
)
```

**What this avoids, named explicitly:**

- **The node's process list.** `sudo -S` is what keeps the secret off `argv`. A
  construction like `echo 'pw' | sudo -S ...` puts the password in the command
  string, which means it is visible to any user on the node via `ps auxww` for
  the lifetime of the command, and it lands in the SSH server's own audit trail
  of executed commands.
- **The remote user's shell history**, for the same reason.
- **`RemoteCommandError`.** `_command_output` (`nodes.py:516-522`) puts
  `result.stderr` into the message the user sees, and that message is saved to
  `NodeProvisionTask.error` in Mongo. `-p ''` suppresses the prompt string;
  the code must additionally never interpolate the password into a
  `describe_failure` message. A test asserts this.
- **`BIOINFO_HOME`-adjacent files.** Nothing is written.

`-p ''` rather than the default prompt: the default prompt text is written to
stderr and would otherwise be the leading content of every failure message the
user reads.

`--` before the command is not cosmetic: it stops a `storage_location` that
begins with `-` from being parsed as a sudo option.

**This requires a new execution path.** `_execute_remote_commands` (:550-554)
hardcodes `conn.run(step.command, check=False)` with no stdin. `RemoteStep`
gains an optional `stdin: str | None = None` field and the runner passes it as
`input=` when set. That is a small, contained change to a shared function, and
it must not alter behaviour for any existing step — every current `RemoteStep`
leaves `stdin` at `None` and `input=` is not passed at all.

## Decision Q3: sudo is a fixed, named set of commands — not a channel

The implementation exposes **no general "run this as root" helper**. There is a
private module-level tuple of the exact commands this feature needs root for,
and the sudo runner refuses anything not built from it.

The complete set:

| # | Command | Why root |
|---|---|---|
| 1 | `sudo -n true` | Probe: is NOPASSWD available |
| 2 | `apt-get install -y cifs-utils` (or the detected package manager's equivalent) | Package install |
| 3 | `install -o root -g root -m 600 /dev/null /etc/bioflow-smb.cred` then write it | Root-only credentials file |
| 4 | `mkdir -p <storage_location>` | Mountpoint may be under a root-owned parent |
| 5 | `mount -t cifs ...` | Mounting |
| 6 | Append one line to `/etc/fstab` | System config |
| 7 | `umount <storage_location>` | Rollback only |

Seven commands. Nothing else in provisioning gains sudo in this ticket.

**Why a named set rather than a general helper.** A general helper is a root
shell reachable from anywhere in `nodes.py`, and the next feature that wants
root will use it without re-doing this analysis — which means this security
review happens once and then never again. A named set forces the next feature to
add its command to a list that a reviewer reads, and makes "what can BioFlow do
as root on my machine?" a question with a finite, greppable answer.

This is a **discipline, not a sandbox**, and the spec should not pretend
otherwise. Item 5's arguments include user-controlled `storage_location`, so the
constraint that actually holds is *shape*, not *harmlessness*. Every
interpolated value is passed through `node_ssh._quote` (`node_ssh.py:196-198`),
and `storage_location` gains the validator it has never had (R-SEC-4).

## Decision Q4: the fstab entry must not be able to brick boot

```
//<primary>/<share>  <storage_location>  cifs  credentials=/etc/bioflow-smb.cred,_netdev,nofail,x-systemd.automount,x-systemd.idle-timeout=600,x-systemd.mount-timeout=30,soft,vers=3.1.1,uid=0,gid=0,file_mode=0664,dir_mode=0775  0  0
```

Every option, and what removing it costs:

- **`_netdev`** — marks the mount as requiring the network. Without it systemd
  orders the mount before networking and it fails at every boot, or on
  non-systemd init hangs waiting for a network that is not up yet.
- **`nofail`** — a failed mount does not fail the boot transaction. **This is
  the single option standing between a decommissioned primary and a node that
  drops to an emergency shell on boot.** Without it, powering off the primary
  bricks every node that mounted from it, and recovering requires physical or
  console access to edit `/etc/fstab`. Say this in the code comment.
- **`x-systemd.automount`** — the mount is attempted on first access rather than
  at boot. Combined with `nofail`, boot never waits on the primary at all. This
  converts "primary is down" from a boot-time problem into a runtime `ENOENT` at
  the moment something reads `/data` — which is exactly the failure the sentinel
  check (`storage/home.py:145-152`) is built to report honestly.
- **`x-systemd.mount-timeout=30`** — bounds how long a single mount attempt
  blocks. The default is systemd's 90s `DefaultTimeoutStartSec`, long enough to
  look like a hang.
- **`x-systemd.idle-timeout=600`** — unmount after ten idle minutes, so a
  primary that went away and came back is re-mounted on next access rather than
  leaving a wedged handle.
- **`soft`** — I/O returns `EIO` after the retry budget instead of blocking the
  calling process in uninterruptible sleep forever. `hard` is the CIFS default
  and it is the wrong default here: a hard mount to a dead primary produces
  D-state worker processes that `docker stop` cannot kill and only a reboot
  clears.
- **`vers=3.1.1`** — pinned. Version negotiation with a macOS `smbd` (the
  primary may be a Mac, per #843) has historically produced silent downgrades;
  a pin makes a mismatch a loud mount failure instead.
- **`uid=`/`gid=`/`file_mode=`/`dir_mode=`** — see Q5.

`soft` has a real cost and the spec names it: a write interrupted by a primary
that vanishes mid-operation can be **partially written and report success at the
syscall layer before a later `EIO`**. `hard` would have blocked instead. This is
the correct trade for a tool whose worst boot outcome must not be an
unrecoverable node, but it means a network blip during a long alignment can
produce a truncated intermediate. `storage/cas.py`'s `os.rename()`-on-completion
placement (:144) limits the damage to staging/tmp, not the object store.

**Rollback.** If the probe fails, the fstab line must be removed, not left
behind. A failed provisioning that leaves an fstab entry pointing at a share
that did not work is worse than no entry.

## Decision Q5: `uid=0,gid=0`, and the share must be treated accordingly

**The worker container runs as root.** `backend/Dockerfile` has no `USER`
directive and no compose file sets `user:`; `docker exec biopipe-api-1 id`
returns `uid=0(root) gid=0(root)`. Verified, not assumed.

So the mount options are `uid=0,gid=0`. CIFS presents every file as owned by
that uid/gid regardless of what the SMB server stored, which is the property
#843 chose SMB for.

**What this means, and it is not nothing.** Every file the node reads or writes
on the shared store is presented as root-owned, and the process touching it is
root. There is no privilege separation between BioFlow's data and the node's
root filesystem *within the container*. The container boundary is the only thing
between them.

This is a **pre-existing property of this project**, not something #848
introduces — the worker has always run as root against a bind-mounted `/data`.
What #848 changes is that the same root now reaches data on *another machine*.
That is worth stating in the spec and worth an issue, but it is not this
ticket's to fix: changing the container's UID is a cross-cutting change touching
the Dockerfile, every compose file, and the ownership of existing
`BIOINFO_HOME` trees. **File it as a separate issue** (see Out of scope).

`file_mode=0664,dir_mode=0775` rather than `0777`: nothing on the node needs
world-write on the shared store, and the modes CIFS synthesises are visible to
anything else that mounts the same share.

**Do not** hardcode `0` as a literal in the mount string. Derive it from a named
constant with a comment pointing at this decision, so that if the container ever
stops running as root the mount options are one edit away and greppable.

## Decision Q6: the mount must be visible to the node's *Docker daemon*

This is the trap `BIOINFO_HOME_HOST` exists to make explicit
(`config.py:299-305`), and it is the most likely way this feature ships broken
while every check reports success.

The chain: `_render_node_compose` bind-mounts `${BIOINFO_HOME}:/data` (:443) and
also sets `BIOINFO_HOME_HOST: ${BIOINFO_HOME}` (:436). A pipeline that starts a
**sibling container** — DeepVariant, SnpEff — does so through the node's mounted
Docker socket, so the path it passes is resolved by the node's **Docker daemon**
against the **node's host filesystem**, not inside the worker container.
`variant_runner.py:213` `host_path_for` is the sole consumer.

**Requirement:** the SMB share must be mounted on the node's host at exactly
`storage_location`, in the mount namespace the Docker daemon runs in.

The failure this guards against: mounting inside a namespace the daemon does not
share. A mount performed over SSH into the node's default namespace satisfies
this. A mount performed inside a container, or in a private namespace (`systemd`
units with `PrivateMounts=yes`, `unshare -m`), does not — and the worker
container's own bind mount would still resolve, because Docker resolved
`${BIOINFO_HOME}` at container-start time. So **the worker sees the data and
sibling containers do not**, and the only symptom is DeepVariant failing on a
missing input.

**How it is verified, and it must be verified rather than reasoned about:** the
`check_storage` phase's probe (#844) must confirm the sentinel is readable
**through a sibling container**, not only through the SSH session. Concretely:

```
docker run --rm -v <storage_location>:/probe alpine cat /probe/.biopipe/VERSION
```

run on the node over SSH. This resolves the bind mount through the node's Docker
daemon by exactly the path `host_path_for` will later produce, so a namespace
mismatch fails here instead of hours into a variant call. An SSH-side `cat` of
the same file does **not** prove this and must not be treated as sufficient.

The image is a concern: pulling `alpine` adds a network dependency to
provisioning. Use the BioFlow backend image, which `pull_image` fetches anyway —
but that means this probe must run **after** `pull_image`, which conflicts with
Q7's cost argument. Resolution in Q7.

## Decision Q7: two phases, split around `pull_image`

`install_key` sits before `pull_image` deliberately, "so a node that cannot take
the key costs nothing" (`nodes.py:821-827`). `pull_image` has a 600s timeout
(:868) and is by far the most expensive phase. Anything that can fail should
fail before it.

But Q6's daemon-visibility check needs an image. Splitting resolves both:

| Phase | Position | What it does |
|---|---|---|
| `mount_storage` | after `install_key` (:828-847), before `pull_image` | sudo probe, install `cifs-utils`, write credentials file, mkdir, mount, fstab entry |
| `check_storage` | after `start_worker` (:873), before `verify` (:892) | #844's round-trip sentinel probe, including the sibling-container read |

`mount_storage` catches everything cheap to catch — no sudo, no package
manager, wrong share name, bad credentials, unreachable primary — before the
600s pull. What remains for `check_storage` is only what genuinely needs an
image on the node.

**`check_storage` is #844's phase and #848 does not invent it.** #844 defines
the phase, the probe and the `storage_shared` field; #848 makes the mount happen
before it runs and adds the sibling-container leg. If #844 landed the probe
elsewhere in the sequence, #848 moves it — which is a change to #844's code and
must be called out in that PR, not done quietly.

**The probe is the gate, not `mount` exiting 0.** A successful mount of the
*wrong export* exits 0 and satisfies every check in `mount_storage`. This is the
entire reason #843 ordered "probe first, automate second" (GROUND.md, decisions).
A `check_storage` failure fails the provision with **the mount error** — naming
the share, the mountpoint and what the sentinel read returned — not a generic
one.

**No `NodeProvisionTask` model change is needed.** `phase` is a free-form `str`
with default `""` and no enum (GROUND.md §B). The frontend's `phaseLabel`
(`SettingsNodes.tsx:423-425`) mechanically renders `mount_storage` as **"Mount
Storage"** with zero frontend changes.

## Decision Q8: idempotency is guard-and-exit, per `ops/` convention

`ops/migrate-storage.sh` is the model (GROUND.md §F): guard-and-exit
preconditions before any mutation, verify before destroying.

Four guards, each a precondition check that makes the corresponding action a
no-op rather than a duplicate:

1. **`cifs-utils`** — check `command -v mount.cifs` first; skip the package
   install if present. Avoids an unnecessary `apt-get` (which can fail on a node
   with no network to its distro mirror even though the SMB share is fine).
2. **Already mounted at the right place** — `findmnt -n -o SOURCE
   --target <storage_location>` and compare to the intended `//host/share`.
   Matching → skip the mount. **Not matching but mounted → refuse**, with the
   remedy naming what is mounted there. Silently unmounting something the user
   put there is not this feature's call.
3. **fstab entry** — match on the *mountpoint field*, not on the whole line. A
   whole-line match duplicates the entry whenever any option changes, which is
   the exact duplication the acceptance criterion forbids. An existing entry for
   this mountpoint is **replaced**, not appended to, so a re-provision with
   corrected options actually corrects them. Write via a temp file and
   `mv`, never an in-place edit that can truncate `/etc/fstab` on a full disk.
4. **Credentials file** — `install -m 600 -o root -g root` then write. Recreated
   unconditionally; it is cheap and it is how a rotated share password takes
   effect.

Guard 3's replace-not-append behaviour needs the fstab line to carry a marker
comment (`# bioflow-managed`) so a hand-written entry for the same mountpoint is
distinguishable from one BioFlow wrote. **BioFlow replaces only its own line**
and refuses if an unmarked entry for that mountpoint exists.

## Decision Q9: `.biopipe/lock` over CIFS — not a blocker, but it changes meaning

`lock_path` (`config.py:637-638`) has exactly one consumer:
`storage/home.py:129` `_acquire_lock`, which does
`fcntl.flock(fd, LOCK_EX | LOCK_NB)`.

**It already tolerates failure.** `home.py:130-141`:

```python
except OSError as e:
    os.close(fd)
    if e.errno in (errno.EACCES, errno.EAGAIN):
        # Advisory locks are unreliable over some FUSE configurations, so a
        # failure here is a warning rather than a hard stop.
        log.warning("home_lock_contended", path=str(settings.lock_path))
        return
```

So CIFS will not crash anything. **But the lock's stated purpose stops holding,
and that is the real finding.** The docstring says "Refuse to run two stacks
against one home directory. Two independent stacks sharing a home would race on
blob refcounts and GC."

Under #843, **every node deliberately shares one home directory** — that is the
entire point of the epic. The condition the lock was written to prevent becomes
the intended configuration. Worse, `flock` over CIFS is not merely unreliable;
depending on the server and `nobrl`, it is commonly *locally scoped*, so each
node acquires "the" exclusive lock successfully and none of them contend. The
warning never fires. The lock silently reports the safe answer while providing
no mutual exclusion.

**Verdict: not a blocker for #848, and a loud finding for the epic.** It is not
a blocker because compute nodes do not run GC or blob refcounting — the primary
does. What must happen:

- **`mount_storage` must not make this worse.** Do not add `nobrl` to the mount
  options; it would make the lock silently succeed everywhere by design.
- **`_acquire_lock` must distinguish a compute node from the primary.**
  `settings.is_compute_node` already exists (`config.py:641-643`). A compute
  node should not take the home lock at all — it has no business claiming
  exclusive use of a directory the epic is explicitly sharing. That is a
  two-line change and **belongs in #848's PR**, because #848 is what makes
  multiple stacks share a home for the first time.
- **The primary's own claim on the lock is now meaningless as a cross-machine
  guarantee** and the docstring must say so rather than continuing to promise
  mutual exclusion it cannot deliver. **File an epic-level issue** for whether
  cross-node coordination of GC needs a real mechanism (a Mongo-backed lease,
  not a filesystem lock).

**`_assert_same_filesystem` is fine.** `objects/`, `staging/` and `tmp/` are all
derived from `bioinfo_home` (`config.py`, :392 section) so on a node they all sit
on the one CIFS mount and share a `st_dev`. `os.rename()` within a single CIFS
mount is supported. Verified by reading `home.py:93-108`; **confirm empirically
on a real mount before implementing** (see Verify).

## Requirements

Permanent identifiers. Never reused.

### Functional

- **R-848-1.** A user provisioning a Linux node with a reachable primary share
  gets `cifs-utils` installed on that node when it is absent.
- **R-848-2.** BioFlow writes the share credentials to a file on the node owned
  by root and mode 0600.
- **R-848-3.** BioFlow creates the mountpoint directory at the node's
  `storage_location` when it does not exist.
- **R-848-4.** BioFlow mounts the primary's share at the node's
  `storage_location`.
- **R-848-5.** BioFlow adds one `/etc/fstab` entry so the mount is restored
  after the node reboots.
- **R-848-6.** The mounted filesystem presents every file as owned by the uid
  and gid the worker container runs as, regardless of the uid the SMB server
  stored.
- **R-848-7.** The node's Docker daemon can bind-mount `storage_location` into a
  container and read the sentinel file through it.
- **R-848-8.** Provisioning reports mount progress through
  `NodeProvisionTask.phase` as `mount_storage`.
- **R-848-9.** A node whose #844 probe fails does not enrol, and the reported
  error names the share, the mountpoint, and what the probe read.
- **R-848-10.** A user re-provisioning an already-mounted node ends with exactly
  one `/etc/fstab` entry for that mountpoint.
- **R-848-11.** A user re-provisioning an already-correctly-mounted node does
  not cause the share to be unmounted or remounted.
- **R-848-12.** A user provisioning a node where a *different* filesystem is
  already mounted at `storage_location` gets a refusal naming what is mounted
  there, and BioFlow does not unmount it.
- **R-848-13.** A node whose fstab entry points at an unreachable primary
  completes boot to a normal login.
- **R-848-14.** A failed `mount_storage` or `check_storage` leaves no BioFlow
  `/etc/fstab` entry on the node.

### Security

Per CLAUDE.md's non-functional table: authentication, data in transit, retention.

- **R-SEC-1** (retention). The sudo credential is not written to the `nodes`
  collection, the `node_provisions` collection, any log record, or any file on
  the primary or the node.
- **R-SEC-2** (retention). The sudo credential does not appear in any
  `NodeProvisionTask.message` or `NodeProvisionTask.error` value.
- **R-SEC-3** (data in transit / exposure). The sudo credential does not appear
  in the argument vector of any process on the node.
- **R-SEC-4** (authorisation scope). BioFlow runs under sudo only commands built
  from the fixed set enumerated in Q3; `storage_location` is validated to an
  absolute path with no shell metacharacters before any interpolation.
- **R-SEC-5** (authentication). BioFlow requests a sudo credential only when
  `sudo -n true` fails on the node.
- **R-SEC-6** (consent). BioFlow uses the SSH password as the sudo password only
  when the user has explicitly opted in.
- **R-SEC-7** (retention). The share credentials file written to the node is
  readable only by root.
- **R-SEC-8** (data in transit). The SMB connection negotiates SMB 3.1.1 or the
  mount fails; it does not silently fall back to SMB1.

## Testing

### The mock gotchas that will bite (GROUND.md §D)

The existing pattern in `backend/tests/api/test_node_provision.py:444-458`:

```python
with patch("app.api.v1.nodes.asyncssh") as ssh, \
     patch("app.services.node_ssh.verify_key", _verify_key_mock()), \
     patch("app.api.v1.nodes._VERIFY_SETTLE_SECONDS", 0), \
     patch("app.api.v1.nodes.asyncssh.scp", AsyncMock()):
```

Four things must be got right or the test lies:

1. **`ssh.connect` must itself be an `AsyncMock`**, not just its `.return_value`
   (:452-456).
2. **`conn.close` must be `MagicMock`, not `AsyncMock`** — #788 (:544-547).
3. **`verify_key` returns a two-tuple.** A bare `AsyncMock` dies inside the
   catch-all with no signal — #444. Use the `_verify_key_mock()` helper (:45-58).
4. **`_routable_primary_hostname`** (autouse, :25-40) patches
   `mod.settings.primary_hostname` because tests run in a container where
   `_primary_hostname()` refuses its own Docker address (#803). **Every new test
   needs it**, and it is autouse in that module only.

Also: `pytestmark = pytest.mark.usefixtures("beanie_models")` and
`asyncio_module_loop = pytest.mark.asyncio(loop_scope="module")` (:14-20); the
loop scope must match `beanie_models`'. There is **no shared provisioned-Node
fixture** — build inline, `await node.delete()`.

### Testing a sudo path without a real node

The whole sudo path is `conn.run` calls, so it tests exactly like every other
phase — assert on the call log (:502-504):

```python
commands = " ".join(str(c) for c in conn.run.call_args_list)
```

The new tests:

- **R-SEC-3, the important one.** With a `sudo_password` supplied, assert the
  password string appears in **no** positional argument of any `conn.run` call,
  and appears **only** in an `input=` kwarg. `call_args_list` carries kwargs, so
  this is directly assertable — and it is the test that actually enforces the
  design rather than describing it.
- **R-SEC-2.** Force a `mount` failure whose stderr echoes the password (a real
  possibility if a future `-p` change reintroduces prompting), and assert the
  saved `NodeProvisionTask.error` does not contain it.
- **R-SEC-5.** With `conn.run("sudo -n true")` mocked to exit 0, assert no
  `input=` is ever passed and provisioning succeeds with `sudo_password=None`.
- **R-SEC-1.** After a successful provision, re-read the `Node` and
  `NodeProvisionTask` documents and assert the password appears in neither
  serialised form. Assert against `model_dump()`, not named fields — a field
  added later is then covered automatically.
- **R-848-10 / R-848-11 (idempotency).** Mock the guard commands to report
  "already mounted, entry present" and assert no `mount`, no `>> /etc/fstab`
  and no `apt-get` in the call log. This is the negative-assertion idiom the
  file already uses for `docker pull` (:502-504).
- **R-848-12.** Mock `findmnt` to report a different source; assert the task
  fails, the error names the found source, and `umount` is absent from the log.
- **R-848-14 (rollback).** Mock the `check_storage` probe to fail; assert the
  fstab-removal command *did* run and the task failed with the mount error.
- **R-848-7.** Assert the `docker run -v <storage_location>:/probe` command is in
  the log and that a non-zero exit from it fails the provision. Patching only the
  SSH-side `cat` to succeed while this fails must still fail the provision — that
  asserts the namespace check is load-bearing rather than decorative.
- **Phase ordering (Q7).** Assert `mount_storage` appears in the phase sequence
  before `pull_image` and `check_storage` after `start_worker`. Assert on the
  recorded phase sequence, not on `task.phase` at the end, which only shows the
  last one.
- **R-848-13 cannot be unit-tested.** It is a property of the fstab options on a
  real machine. Assert the *string* contains `_netdev`, `nofail` and
  `x-systemd.automount` — a cheap regression guard for the option most likely to
  be dropped in a later edit — and verify the behaviour manually (see Verify).

### Not unit-testable, must be done on real hardware

R-848-13 (boot with the primary off), R-SEC-8 (protocol negotiation against a
real macOS `smbd`), Q9's `flock` behaviour over a real CIFS mount, and
`_assert_same_filesystem` over a real CIFS mount.

## Verify before implementing

1. **`_assert_same_filesystem` over a real CIFS mount.** Mount a share, `stat`
   `objects/`, `staging/`, `tmp/` and confirm one `st_dev`. If it is not, node
   startup raises `StorageUnavailableError` (`home.py:100-107`) and this feature
   cannot ship as designed.
2. **`fcntl.flock` over a real CIFS mount, from two nodes at once.** Confirm
   whether both acquire it (locally scoped) or the second gets `EAGAIN`. This
   determines how loudly Q9's epic-level issue needs to be written.
3. **`os.rename()` within a CIFS mount** where the target exists —
   `storage/cas.py:144` depends on it, and `cas.py:140` `chmod 0o444` before it.
   Confirm CIFS with `file_mode=0664` does not reject the chmod.
4. **`vers=3.1.1` against the primary's actual `smbd`**, macOS and Linux both.
   If macOS's stock `smbd` will not negotiate 3.1.1, the pin must change and Q4
   needs an update.
5. **The package name and manager.** `cifs-utils` on Debian/Ubuntu; RHEL family
   uses `dnf` and the same package name. Decide whether to detect the manager or
   restrict to `apt-get` and refuse otherwise with a remedy — **a refusal is
   preferable to a wrong guess**, matching `verify_docker`'s posture.
6. **`sudo -n true` exit codes.** Confirm it exits non-zero rather than
   prompting when NOPASSWD is not configured, including when the user is not in
   `sudoers` at all — the two cases need different remedy text.
7. **asyncssh `input=` reaches a real `sudo -S`.** The signature and docstring
   confirm the kwarg is forwarded to `create_process` (verified on 2.24.0), but
   confirm end to end against a real sshd that sudo actually reads it and that
   the channel is not closed before sudo reads stdin.
8. **`findmnt` availability.** Present in `util-linux` on any modern Linux, but
   confirm and pick a fallback (`/proc/mounts`) if a target distro lacks it.

## Out of scope

- **macOS nodes.** `mount_smbfs` plus a LaunchAgent, no `/etc/fstab`. A separate
  client implementation (#843 scope boundary, GROUND.md §F notes the two differ
  substantially).
- **Configuring the share on the primary** — #847, a hard dependency.
- **Tearing down the mount when a node is removed** — #850. Q1 makes a decision
  that constrains it; #850 should cite Q1 rather than re-open it.
- **The probe itself** — #844 defines `check_storage`, the round trip and the
  `storage_shared` field. #848 orders the mount before it and adds the
  sibling-container leg.
- **Excluding non-shared nodes from work** — #845.
- **Migrating existing hand-configured nodes** — #846.
- **The helper script for non-launcher installs** — #849.
- **Changing the container's UID from root** (Q5). Cross-cutting: the
  Dockerfile, every compose file, and the ownership of existing `BIOINFO_HOME`
  trees. **File as its own issue.**
- **Cross-node GC coordination** (Q9). The home lock's promise does not survive
  a genuinely shared home. **File as its own epic-level issue.**
- **Generalising sudo to other provisioning steps.** Q3 constrains this
  deliberately; installing Docker via sudo is a separate decision with its own
  argument, and `verify_docker`'s refuse-and-instruct posture is not changed
  here.
- **Encrypting SMB traffic in transit.** SMB 3.x supports encryption
  (`seal` mount option) at a real throughput cost. On a LAN carrying
  genomic data between machines the user owns, the trade is not obvious and
  deserves its own decision.
