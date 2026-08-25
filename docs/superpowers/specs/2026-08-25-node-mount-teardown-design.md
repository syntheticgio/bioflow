# Tearing down a node's mount when the node is removed — design

Date: 2026-08-25.

Closes [#850](https://github.com/syntheticgio/bioflow/issues/850). Child 7 of
[#843](https://github.com/syntheticgio/bioflow/issues/843).

**Depends on [#848](https://github.com/syntheticgio/bioflow/issues/848)** —
there is nothing to tear down until #848 mounts something.

Assumed delivered by #848, per its design
(`docs/superpowers/specs/2026-08-25-node-smb-mount-design.md`): a
`sudo_password` field on `ProvisionRequest` that is **never persisted** (its
Q1), an `/etc/fstab` line carrying a `# bioflow-managed` marker (its Q8 guard
3), a root-owned credentials file at `/etc/bioflow-smb.cred` (its Q3 item 3),
an installed `cifs-utils`, and a mountpoint at the node's `storage_location`.

## The finding that reframes this ticket

The ticket reads as "reverse #848's client-side changes." Verifying the current
lifecycle turned up something larger, and the spec would be dishonest to bury
it.

**Revocation does nothing.** `DELETE /nodes/{node_id}` → `revoke_node`
(`backend/app/api/v1/nodes.py:1073-1086`) is six lines: find the doc, set
`status = "revoked"`, save, log, return. It does not delete the document, wipe
`ssh_key_enc`, open an SSH connection, run `docker compose down`, remove
`~/.bioflow`, strip the `authorized_keys` line it installed, or purge the
node's Redis keys. There is no decommission or unprovision endpoint anywhere in
the API. Confirmed by reading the file and by GROUND.md §B.

Two consequences shape every decision below.

1. **There is no existing remote-teardown machinery to extend.** This ticket is
   not adding a step to a cleanup path; it would be creating the first one.
2. **`revoke_node` has no frontend caller.** `frontend/src/api/client.ts`
   exposes `provision`, `provisionStatus`, `currentVersion`, `update` and
   `updateStatus` for nodes (`:567-587`) — no delete. `SettingsNodes.tsx`'s
   Actions column renders only an Update button (`:538-556`). The only way to
   revoke a node today is to call the API by hand.

So "removing a node" is, in the product as it stands, an operation with no UI
and no remote effect. Building mount teardown as a remote-execution subsystem
hung off that endpoint would be building the most privileged thing BioFlow does
to a machine, attached to the least-exercised path in the product.

## What exists today

Verified against this worktree on 2026-08-25.

- **`revoke_node`** — `backend/app/api/v1/nodes.py:1073-1086`. Flips
  `status = "revoked"`, saves, returns `{"node_id", "status"}`. Fast, local,
  cannot fail except on a missing node (`NotFoundError`).
- **Revocation is pull-based and advisory.** The worker discovers it on its own
  schedule: `_enrollment_watch_loop` (`backend/app/queue/worker.py:643-677`)
  sleeps 30s, polls `GET /nodes/{id}/status`, and treats a 404 as "re-enroll."
  `_enroll` (`:605-641`) is explicitly advisory — its failures are non-fatal.
  Nothing in the revoke path blocks on the node acknowledging anything.
- **The `Node` document** (`backend/app/models/node.py`, 57 lines) carries
  `node_id`, `hostname`, `status` ("active"|"revoked"), `ssh_host`, `ssh_port`,
  `ssh_username`, `ssh_key_enc` (Fernet), `ssh_key_installed_at`, `host_key`
  (TOFU), `image_digest`, `version`. **There is no `storage_location` field** —
  the value the user typed at provisioning went into the node's `.env` and was
  then forgotten (GROUND.md §B).
- **`ssh_key_enc` is decrypted on demand** by
  `backend/app/services/node_update_service.py:87`, which is why it is
  persisted at all: node updates need a recurring capability.
- **`NodeProvisionTask`** (`backend/app/models/node_provision.py`, 33 lines)
  is the precedent for a long-running remote operation surfaced to the UI:
  `task_id`, `status`, `phase` (**free-form str, no enum, default `""`** — a new
  phase needs no model change), `message`, `pct`, `error`, timestamps.
- **`_execute_remote_commands`** (`nodes.py:545-554`) is the remote-step runner:
  one `on_progress` call per distinct `phase`, `conn.run(..., check=False)`,
  non-zero exit raises `RemoteCommandError` carrying `.step` and `.reason`.
- **The refusal shape to copy** is `UnroutablePrimaryHost` (`nodes.py:280-281`,
  raised `:348-392`, caught `:742-751`): what was found → why it is wrong →
  the exact remedy → "provision the node again."

## Decision Q1: a separate `decommission` operation, not a change to revoke

`revoke_node` keeps its current semantics unchanged. Teardown is a **new,
separate, explicitly-invoked operation**: `POST /nodes/{node_id}/decommission`,
returning a task id and polled like provisioning.

Rejected alternatives, and why.

**Extend `revoke_node` to perform teardown.** Revocation is currently a
sub-millisecond local write with exactly one failure mode. Teardown is an SSH
connection, a sudo authentication and up to four privileged commands against a
machine that may be off. Folding one into the other changes `DELETE /nodes/{id}`
from an operation that always succeeds into one that usually takes seconds and
sometimes fails — and the caller most likely to be hurt is the user whose node
is already dead, which is the ticket's own stated common case. It also gives
`DELETE` a request body (the sudo credential), which is the shape of an
operation that should not have been a `DELETE`.

**A boolean `teardown=true` flag on revoke.** Cheaper to build, but it makes
one endpoint two operations with different latency, different failure modes and
different progress semantics, distinguished by a query parameter. The
provisioning path already established that a slow, phased, possibly-failing
remote operation gets its own endpoint and its own task document; teardown is
that same shape and should look like it.

**Separate endpoint, and revoke first.** Decommission **requires the node to
already be revoked** and refuses otherwise. That ordering is deliberate: the
cheap, reliable, always-succeeds step that stops the node claiming work happens
first and independently, and the slow privileged step is a second, optional act
the user chooses. A user whose node caught fire runs revoke and stops. A user
reclaiming a laptop runs revoke, then decommission.

The task document is a **new `NodeDecommissionTask`**, not a reused
`NodeProvisionTask`. They share a shape but not a meaning, and
`_clean_node_provisions` and the provisioning list endpoints would otherwise
start reporting teardowns as provisionings.

## Decision Q2: the sudo credential is supplied at teardown time, and teardown is optional

**This is the decision most in need of the user's explicit approval**, because
it accepts a real cost rather than engineering it away.

#848's Q1 established that the sudo credential is never persisted, and stated
the resolution for this ticket: "#850's correct shape is: prompt for a sudo
credential at teardown time if the user wants the mount removed, and otherwise
print the two commands for them to run." This spec adopts that, and records the
reasoning rather than merely citing it.

**The tension is genuine.** Unmounting and editing `/etc/fstab` need root. At
teardown time BioFlow holds no root credential for the node, because #848
deliberately did not keep one. So teardown cannot be automatic. One of three
things must be true, and the spec must pick.

1. **Persist the sudo credential at provisioning.** Rejected. The asymmetry is
   decisive: the stored root credential is dangerous continuously, for the
   entire life of the node — months — while the capability it buys is a single
   unmount at an unpredictable future moment. `ssh_key_enc` is persisted because
   node updates need it repeatedly (`node_update_service.py:87`); a root
   password for a node would be persisted because *one future delete might want
   it*. That is not the same trade, and BioFlow would be holding root on every
   machine it has ever provisioned in order to be tidy on the way out.
2. **Accept that teardown never runs, and only print instructions.** Rejected as
   the *only* behaviour, because the user who is standing at the node and has
   the password should not be made to leave the product to finish the job.
3. **Prompt at teardown time; fall back to instructions.** Chosen.

So: `DecommissionRequest` carries the same three-source sudo resolution #848
defined — NOPASSWD probe first (`sudo -n true`), then an explicitly supplied
`sudo_password`, then the SSH password only behind an explicit tick box — and
if none is available, **decommission does not fail**. It completes, removes the
node from BioFlow, and reports the exact commands the user must run on the node
themselves.

**The honest cost, stated plainly:** a user who removes a node without supplying
a sudo credential leaves a CIFS mount and an fstab entry on that machine, and
BioFlow's only remedy is a paragraph of text. That is acceptable because the
residue is inert — #848's Q4 mount options (`nofail`, `_netdev`,
`x-systemd.automount`, `soft`) mean a stale entry pointing at a primary that has
stopped exporting does not hang boot — but it is a real thing left behind and
the UI must say so rather than reporting a clean removal.

**Approval needed:** this locks in "BioFlow cannot always finish what it
started" as the accepted posture for privileged node config.

## Decision Q3: BioFlow's record is never held hostage to the node

The ticket is explicit and this spec makes it structural rather than
best-effort.

**The BioFlow-side removal happens first and unconditionally, before any SSH is
attempted.** Not after, not conditionally, not in a `finally`. The order is:

1. Verify the node is revoked. (Refuse otherwise — Q1.)
2. **Delete the `Node` document and purge the node's Redis keys.** Local, fast,
   cannot fail on account of the remote machine.
3. *Then* attempt remote teardown, with a bounded connect timeout.
4. Record the outcome on the decommission task.

Putting step 2 first is the whole mechanism. If SSH is placed first — even
wrapped in a `try` — every future edit to the remote path is one `raise` away
from stranding the record, and the failure only shows up when someone tries to
remove a dead node. Doing the local work first means the remote half is
*structurally* unable to block it: there is no code path in which a remote
failure precedes the deletion.

**The connect attempt is bounded.** A node that is powered off refuses fast; a
node that is on a network black hole does not. The SSH connect gets an explicit
timeout (10s, matching nothing else in the file only because nothing else
connects to a machine expected to be gone) and the whole remote phase gets its
own cap. Unreachable is a normal outcome, reported as one — not an error.

**What the user is told.** Three distinct outcomes, three distinct messages;
the tri-state Status badge in `SettingsNodes.tsx:515-521` is the established
idiom for exactly this.

| Outcome | Node record | Message |
|---|---|---|
| Reachable, sudo available, teardown ran | removed | "Removed. The share was unmounted and the fstab entry deleted." |
| Reachable, no sudo credential | removed | "Removed from BioFlow. The node still mounts the share. Run these commands on it: …" |
| Unreachable | removed | "Removed from BioFlow. `<host>` could not be reached, so nothing was cleaned up on it. It still mounts the share and has an fstab entry. If you get it back, run: …" |

The commands printed are the literal ones, per `ops/` convention (GROUND.md §F:
"Messages include the literal remedy command"), with the node's actual
mountpoint substituted — which requires Q6's recorded `storage_location`.

**Completing teardown later.** Yes, but not by keeping the record. A
`POST /nodes/decommission/manual` that takes host, username, credential and
mountpoint and runs the same teardown steps against a node BioFlow no longer
knows about is the shape that does not compromise Q3 — the record is already
gone, and this is a utility that happens to operate on a machine. **Recommended
as a follow-up issue, not built here**, because the printed commands already
solve the case and this adds an SSH-to-arbitrary-host endpoint that deserves its
own review.

## Decision Q4: teardown cannot delete data, by construction

This is the ticket's most important requirement and the one that must not rest
on intent.

**The rule: teardown never issues a command that removes a file path under the
mountpoint. Not `rm`, not `rm -rf`, not `find -delete`, not `shred`, not a
`mkdir -p` that could race a removal.** The complete set of privileged commands
this feature runs is:

| # | Command | Touches |
|---|---|---|
| 1 | `sudo -n true` | nothing |
| 2 | `umount <storage_location>` | the mount, not its contents |
| 3 | remove the `# bioflow-managed` fstab line for that mountpoint | `/etc/fstab` |
| 4 | `rm -f /etc/bioflow-smb.cred` | one file, at a **fixed literal path outside the mountpoint** |

Four commands. No fifth. This mirrors #848's Q3 named-set discipline and, as
there, it is a discipline rather than a sandbox — but here the discipline is
much stronger, because unlike #848 **no command in the set takes a path
argument that could point into the share.** Command 2's argument is the
mountpoint itself and `umount` has no destructive mode. Command 4's path is a
compile-time constant with no interpolation.

**The ordering guard.** The dangerous scenario the ticket names is a cleanup
that runs after a failed unmount and deletes through a still-mounted share.
That scenario cannot arise here because no step deletes anything under the
mountpoint — but the ordering is still specified, because a future edit is the
real threat:

- Command 4's path (`/etc/bioflow-smb.cred`) is asserted **not** to be under
  the mountpoint before it runs. A `storage_location` of `/` or `/etc` would
  otherwise make an innocuous command destructive.
- Nothing is attempted after an unmount failure that assumes the unmount
  succeeded. If `umount` fails, teardown reports the mount as still present and
  moves on to the fstab line — which is correct and safe, since removing the
  fstab entry of a currently-mounted filesystem affects only the next boot.
- **`umount -l` (lazy) and `umount -f` (force) are not used.** A busy mount
  means something on the node is reading the primary's files right now. Forcing
  it out from under a running process is not this operation's call; report
  "still in use" and let the user deal with it.

**The test that makes it structural.** A test asserts that the module's command
set contains no destructive verb and that no command's arguments derive from a
path under the mountpoint — a source-level assertion over the named tuple, in
the manner CLAUDE.md prescribes for a hand-maintained registry. A reviewer
should be able to answer "can this delete my data?" by reading one tuple.

**`storage_location` is validated before it is used**, reusing the validator
#848 adds (its R-SEC-4). A mountpoint of `/`, `/etc`, `/usr`, `/home`, `/boot`
or `/var` is refused outright rather than unmounted.

## Decision Q5: teardown removes the mount and nothing else — and the rest is a filed issue

The four commands in Q4 are the whole scope. Teardown does **not** run
`docker compose down`, remove `~/.bioflow`, strip the `authorized_keys` line,
or wipe `ssh_key_enc` from the document.

Except it does wipe `ssh_key_enc` — because Q3 deletes the whole document, which
removes it as a side effect. That is worth naming so nobody later "fixes" the
deletion into a soft-delete and silently reintroduces a stored key for a machine
BioFlow no longer manages.

The rest is deliberately excluded here and **should be filed as its own issue**,
because it is a different problem with a different risk profile:

- Stopping the worker and removing `~/.bioflow` is *reversible cleanup of
  BioFlow's own footprint*, needs no root, and is arguably what "remove a node"
  should have meant all along.
- Removing the `authorized_keys` line is *revoking BioFlow's access*, which is a
  security operation and the most valuable of the three.
- Neither depends on #843, #847 or #848. Both are blocked today only by the fact
  that no remote-teardown path exists — which this ticket creates.

Scoping them here would mean a ticket about SMB mounts quietly becoming the
ticket that rewrote node lifecycle, with a scope no reviewer signed up for.
**Recommendation: file "decommission should also stop the worker, remove
`~/.bioflow`, and revoke the SSH key" as a follow-up**, referencing the
machinery this ticket builds. It should be filed regardless of whether it is
scheduled — the gap is real and currently undocumented.

**Also excluded: disabling the primary's share.** That is #847's off switch, per
the ticket's own scope boundary. Removing the last node does not stop the
primary exporting.

## Decision Q6: `storage_location` becomes a recorded fact on `Node`

Teardown needs to know what to unmount. Today the primary does not know
(GROUND.md §B: "There is NO `storage_location` field on Node"). It cannot be
recovered from the node without connecting to it, which is precisely the case
teardown must handle without connecting.

Add `storage_location: str | None = None` to `backend/app/models/node.py`,
written at provisioning. Nullable because every node provisioned before this
lands has no value, and a node whose value is unknown gets the "we do not know
what to unmount" message rather than a guess.

**Coordination:** #844's spec adds shared-storage fields to the same model, and
#848 needs the same value. Whichever lands first adds the field; the others use
it. Adding it twice is a merge conflict, which is the good outcome — adding it
under two different names is not.

⚠️ Per GROUND.md §E, a new field read in the `enumerate_nodes` Mongo loop
(`nodes.py:98-106`) can silently empty `mongo_nodes` for stale fixtures. Read it
with `.get()` semantics, not attribute access on a raw dict.

## Decision Q7: idempotency is guard-and-exit, mirroring #848's Q8

A node that was never mounted, or was already torn down, must produce a clean
success and not an error. Following `ops/migrate-storage.sh` (GROUND.md §F) and
#848's Q8, each action is preceded by a precondition check that makes it a
no-op rather than a failure:

1. **Not mounted** — `findmnt -n -o SOURCE --target <storage_location>` returns
   nothing, or a source that is not the expected `//<primary>/<share>`. Skip the
   unmount. A *different* filesystem mounted there is reported and left alone,
   symmetrically with #848's guard 2 refusing to mount over it.
2. **No fstab entry** — no `# bioflow-managed` line for that mountpoint. Skip.
   An **unmarked** entry for that mountpoint is left alone and reported: BioFlow
   did not write it and does not remove it.
3. **No credentials file** — `rm -f` is already idempotent; it is used precisely
   so a missing file is not an error.
4. **`cifs-utils` is not uninstalled.** Removing a package the node may have had
   before BioFlow arrived, or may want after, is not reversal — it is collateral.
   #848 installs it only when `command -v mount.cifs` is absent, and teardown
   does not track which case applied. Say this in the result message.

Idempotency also covers the *second* decommission of the same node: the record
is gone, so `POST /nodes/{node_id}/decommission` returns `NotFoundError`. That
is correct and needs no special case.

## Requirements

Permanent identifiers. Never reused.

### Functional

- **R-850-1.** A user can decommission a revoked node, and the operation removes
  the node's record from BioFlow.
- **R-850-2.** Decommissioning a node that is not revoked is refused, with a
  message naming revocation as the prerequisite.
- **R-850-3.** When the node is reachable and a sudo credential is available,
  decommissioning unmounts the share at the node's recorded `storage_location`.
- **R-850-4.** When the node is reachable and a sudo credential is available,
  decommissioning removes the `# bioflow-managed` `/etc/fstab` line for that
  mountpoint.
- **R-850-5.** When the node is reachable and a sudo credential is available,
  decommissioning deletes `/etc/bioflow-smb.cred`.
- **R-850-6.** When the node is unreachable, the node's record is still removed
  from BioFlow.
- **R-850-7.** When the node is unreachable, the result names the host that
  could not be reached and lists what remains on it.
- **R-850-8.** When no sudo credential is available, the node's record is still
  removed from BioFlow.
- **R-850-9.** When no sudo credential is available, the result lists the exact
  commands the user must run on the node, with the node's real mountpoint
  substituted.
- **R-850-10.** Decommissioning a node that is not mounted completes
  successfully without attempting an unmount.
- **R-850-11.** Decommissioning a node with no BioFlow fstab entry completes
  successfully without editing `/etc/fstab`.
- **R-850-12.** A node's `storage_location` is recorded on its `Node` document
  at provisioning time.
- **R-850-13.** A node whose `storage_location` is unrecorded is decommissioned
  with its record removed and a result stating that the mountpoint is unknown.

### Safety

- **R-850-14.** The set of privileged commands decommissioning can run contains
  no command that removes a file path under the node's mountpoint.
- **R-850-15.** Decommissioning refuses a `storage_location` of `/`, `/etc`,
  `/usr`, `/home`, `/boot` or `/var`.
- **R-850-16.** Decommissioning does not use `umount -f` or `umount -l`.
- **R-850-17.** When the unmount fails, decommissioning reports the mount as
  still present rather than proceeding as though it succeeded.
- **R-850-18.** The node's record is deleted before any SSH connection to the
  node is attempted.
- **R-850-19.** An `/etc/fstab` entry for the mountpoint that does not carry the
  `# bioflow-managed` marker is left in place and reported.
- **R-850-20.** A filesystem mounted at the node's `storage_location` whose
  source is not the expected share is left mounted and reported.
- **R-850-21.** The sudo credential supplied to decommissioning is not written
  to Mongo, to a log, or to the decommission task's error field.
- **R-850-22.** The sudo credential reaches `sudo` over stdin and never appears
  in a command string.

## Testing

Conventions from GROUND.md §D. `pytestmark = pytest.mark.usefixtures(
"beanie_models")`, `asyncio_module_loop` with `loop_scope="module"`, and the
three documented mock gotchas: `ssh.connect` must itself be an `AsyncMock`,
`conn.close` must be a `MagicMock` not an `AsyncMock` (#788), and `verify_key`
returns a two-tuple (#444). Nodes are built inline and deleted; there is no
shared provisioned-node fixture.

- **R-850-18 is the load-bearing test.** Patch `asyncssh.connect` to raise
  immediately, then assert `Node.find_one(...)` returns `None` afterwards.
  Additionally assert *ordering*: with a connect mock that records call time
  against a document-deletion spy, the deletion must be recorded first. A test
  that only checks the end state passes for an implementation that deletes in a
  `finally`, which is the implementation this requirement rules out.
- **R-850-14 is a source-level test, not a behaviour test.** Assert over the
  module's command tuple that no entry contains `rm -r`, `rm -f -r`, `find`,
  `-delete`, `shred` or `mkfs`, and that the one `rm -f` entry's path is the
  literal `/etc/bioflow-smb.cred`. This is the registry-shaped assertion
  CLAUDE.md prescribes; it survives refactors that a behaviour test would not.
- **R-850-15** — a `storage_location` of `/` on the node document produces a
  refusal, with the record *not* deleted (this is the one refusal that precedes
  deletion, because the node doc is the source of the bad value).
- **R-850-6/7** — connect raises `OSError`; assert record gone, assert the
  result message contains the host and the words describing what remains.
- **R-850-9** — no sudo credential and NOPASSWD probe fails; assert the result
  contains the literal `umount <the node's actual storage_location>` string.
  Assert against the *node's* value, not a hardcoded default, or the test passes
  while the message tells the user to unmount someone else's path.
- **R-850-10/11** — `findmnt` mock returns empty, fstab grep finds no marked
  line; assert via the `conn.run` call log (GROUND.md §D pattern) that
  `"umount"` and any fstab write do **not** appear. Assert absence, not
  success — a run that succeeds having done nothing and a run that succeeds
  having done the wrong thing both look green otherwise.
- **R-850-19/20** — an unmarked fstab line, and a foreign mount source; assert
  both are left alone *and* named in the result.
- **R-850-21/22** — provide a distinctive sudo password, run a teardown that
  fails at the unmount step, and assert the literal password string appears in
  neither the persisted `NodeDecommissionTask.error` nor any `conn.run` command
  argument. #848's equivalent test is the model.
- **R-850-2** — an active node refuses; assert the record survives.
- **R-850-13** — `storage_location` is `None`; assert removal succeeds and no
  `umount` is attempted.
- **Real-hardware check** — decommission a real provisioned Linux node, then
  reboot it and confirm it boots normally and no longer mounts the share.
  Neither half is unit-testable.

## Verify before implementing

1. **Whether #848 landed the `storage_location` field on `Node`, and under what
   name.** #844 and #848 both plausibly add it. Adding a second field with a
   different name is the failure to avoid (Q6).
2. **The exact fstab marker string #848 writes.** This spec assumes
   `# bioflow-managed`; the removal must match what was written character for
   character, and a mismatch fails silently by leaving the entry behind.
3. **The exact credentials file path #848 uses.** This spec assumes
   `/etc/bioflow-smb.cred` from its Q3.
4. **`findmnt`'s availability and output on the target distributions.** It ships
   with `util-linux` and is present on Debian/Ubuntu/RHEL, but confirm on a real
   node, and confirm the exit status when the target is not a mountpoint —
   the guard branches on it.
5. **What `umount` exits with on a busy mount**, and that the message reaches
   the user intelligibly through `_command_output` (`nodes.py:516-522`,
   stderr→stdout→"no output").
6. **Whether the frontend gets a Remove button in this ticket or a follow-up.**
   There is none today (`SettingsNodes.tsx:538-556`), and shipping a
   decommission endpoint with no caller repeats the gap this spec found. The
   recommendation is to add both a Remove and a Decommission affordance here;
   confirm that is wanted before building UI this ticket did not ask for.

## Out of scope

- **Disabling the primary's share.** #847's off switch, per the ticket.
- **macOS nodes.** #843 scopes the epic to Linux clients; `mount_smbfs` and a
  LaunchAgent are a separate client implementation and a separate teardown.
- **Uninstalling `cifs-utils`** (Q7 item 4).
- **Stopping the worker, removing `~/.bioflow`, and stripping the
  `authorized_keys` line** (Q5) — recommended as a filed follow-up issue.
- **`POST /nodes/decommission/manual`** for a node BioFlow has already forgotten
  (Q3) — recommended as a filed follow-up issue.
- **Changing `revoke_node`'s behaviour** (Q1). It stays six lines.
- **Deleting anything inside `BIOINFO_HOME`** on the primary or the node (Q4).
- **The `.biopipe/lock` semantics under shared storage** — #848's Q9 covers it
  and files the epic-level follow-up.
