# Node shared-storage probe — design

Date: 2026-08-25.

Closes [#844](https://github.com/syntheticgio/bioflow/issues/844). Child 1 of
[#843](https://github.com/syntheticgio/bioflow/issues/843).

Detection and recording only. Excluding non-shared nodes from filesystem work is
[#845](https://github.com/syntheticgio/bioflow/issues/845); mounting the share is
[#848](https://github.com/syntheticgio/bioflow/issues/848).

## Why this is the first child

Chunked alignment fans out one `align_reads` sub-job per bucket with no
`target_node` (`queue/chunked_align_handlers.py:56`), so any node may claim any
bucket. That is correct only if every node reads the same `BIOINFO_HOME`. Today
nothing establishes it. The user types a `storage_location`, provisioning goes
green, and the mistake surfaces hours later as "Input reads not found" on one
bucket of a long alignment — a message that names the file and not the cause.

The probe is also the success check the automation in #847-#850 would itself
use, so building it first means the automated path is verified by something that
has already caught real mistakes.

## What exists today

Verified against this worktree on 2026-08-25.

- **`_provision_node`** (`backend/app/api/v1/nodes.py:671`) runs eleven phase
  transitions in sequence: `validate_ssh` (:705), `write_env` (:750,
  failure-only), `verify_docker` (:768), `setup_install` (:779, :789),
  `write_env` (:802), `install_key` (:828), `pull_image` (:861), `start_worker`
  (:873), `verify` (:892), `enrolled` (:899).
- **`NodeProvisionTask.phase` is a free-form `str`, default `""`**
  (`backend/app/models/node_provision.py`) — no enum, so a new phase string
  needs no model change.
- **`_execute_remote_commands`** (`nodes.py:525`) reports **one `_update` per
  phase, not per command** (:545-549). Its docstring (:541-543) records that "a
  step's phase string is a user-visible contract, not an internal label."
- **The #803 precedent**: `UnroutablePrimaryHost` (`nodes.py:280-281`, raised in
  `_primary_hostname()` :348-392) refuses to provision rather than produce a
  worker that crash-loops while every step reports success. It is caught at
  :742-751, it **reuses the `write_env` phase rather than inventing one**, and
  its message follows the shape *what was found → why it is wrong → exact remedy
  → "provision the node again."*
- **`install_key` is deliberately before `pull_image`** (`nodes.py:821-827`), so
  "a node that cannot take the key costs nothing." `pull_image` has a 600s
  timeout.
- **`_render_node_env`** (`nodes.py:459-478`) writes `storage_location` into
  exactly two keys, `BIOINFO_HOME` and `BIOINFO_REGISTER_ROOTS`. No quoting, no
  escaping, no validation. `ProvisionRequest.storage_location`
  (`nodes.py:40-56`) defaults to `/data/scratch` and has **no validator**.
- **`Node` has no `storage_location` field** (`backend/app/models/node.py`, 57
  lines). The user's value goes into the node's `.env` and the primary keeps no
  record of it.
- **`settings.sentinel_path`** (`backend/app/config.py:632-634`) is
  `$BIOINFO_HOME/.biopipe/VERSION`, docstring "Proves the drive is actually
  mounted." Written by `storage/home.py:_write_sentinel()`, checked by
  `check_home()`. **Its content is the module constant
  `SENTINEL_CONTENT = "biopipe-home-v1\n"`** (`storage/home.py:26`) — a fixed
  string, written only when the file is absent. This fact is decisive for Q1.
- **`settings.lock_path`** (`config.py:637-638`) is `.biopipe/lock`.
  `_acquire_lock()` (`home.py:120-143`) takes a `fcntl.flock` and **already
  degrades to `log.warning` on `EACCES`/`EAGAIN`**, with a comment that advisory
  locks are unreliable over some FUSE configurations.
- **`enumerate_nodes`** (`nodes.py:81-114`) reads Node fields inside a
  `try/except Exception` that discards *all* accumulated `mongo_nodes` on any
  error, including an `AttributeError` from a field a fixture lacks. The comment
  at :107-114 records that this already silently emptied two pre-existing tests.
- **`update_node`** (`nodes.py:999-1033`) is the existing precedent for a
  re-runnable SSH operation against an enrolled node: `POST
  /nodes/{node_id}/update`, refuses with `ConflictError` when `ssh_key_enc is
  None`, inserts a task document, runs it in a background `asyncio.Task`, polled
  via `GET /nodes/update/{task_id}`.
- **`node_ssh.connect_with_tofu`** (`node_ssh.py:73`) returns
  `(conn, host_key)` and enforces the stored host key when one is given.
  `node_ssh._quote` (:196) single-quotes a value for a POSIX shell.
- **Frontend**: `SettingsNodes.tsx:423-425` `phaseLabel` mechanically
  prettifies any phase string — `check_storage` renders as **"Check Storage"**
  with zero frontend change. The node table's columns are at :144-152. `NodeRow`
  (~:495-555) already has an Online/Offline/**Unknown** tri-state Status badge
  with an explanatory `title` (:515-521).

## Decision Q1: write a probe-specific sentinel with a per-probe nonce; do not reuse `.biopipe/VERSION`

The existing sentinel cannot answer this question. `SENTINEL_CONTENT` is the
literal string `"biopipe-home-v1\n"` (`storage/home.py:26`), and
`_write_sentinel()` writes it verbatim on every home that has ever been
initialised. Two machines that each ran `initialize_home()` against their own
local `/data/scratch` hold **byte-identical** `.biopipe/VERSION` files. A probe
that reads `$storage_location/.biopipe/VERSION` on the node and compares it to
the primary's would report `storage_shared = true` for the exact case #843
exists to catch: a node with an identically-named local directory.

So the probe writes its own file:

- **Path**: `$BIOINFO_HOME/.biopipe/probe-<token>` on the primary, read back at
  `$storage_location/.biopipe/probe-<token>` on the node.
- **Content**: a fresh `uuid4().hex` (or `secrets.token_hex`) generated per
  probe, never reused, plus the primary's node identity for a human reading a
  stray file.
- **Two independent coincidences** must both be beaten for a false positive: the
  node would have to hold a file at a path named by a token generated moments
  ago *and* containing that same token. Neither is producible by an
  identically-named local directory.

Putting it under `.biopipe/` rather than at the home root reuses the directory
`initialize_home()` already creates (`config.py:628-630`, `meta_dir`) and keeps
the probe out of the user-visible object tree.

**The primary writes with normal Python file I/O, the node reads over the
existing SSH connection** — `cat` of the path, compared to the token in Python.
Not `test -f`: existence proves a path, content proves the filesystem.

**Symmetry is not needed.** The probe does not have to prove the node can
*write*; #845's exclusion rule is about reading the primary's inputs, and a
read-only share is a legitimate configuration. A write probe would fail a
correct read-only setup, which is a worse error than the one it prevents.

`.biopipe/VERSION` keeps its job. It is a mount check, and it is a correct one;
it is simply not an identity check, and Q1 is an identity question.

## Decision Q2: `check_storage` runs immediately after `write_env`, before `install_key`

Two constraints pull in opposite directions and both are satisfiable.

**It must precede `pull_image`.** The `install_key` comment (`nodes.py:821-827`)
is the argument: a node that fails a cheap check should cost nothing, and
`pull_image` costs up to 600 seconds. A storage failure discovered after a
ten-minute image pull is the same waste that comment exists to prevent.

**It must follow `write_env`, not precede it.** The tempting objection is that
writing a `.env` pointing at unmounted storage is itself wrong. It is not wrong
in any way that matters here: `.env` is an inert file in `~/.bioflow`, nothing
reads it until `start_worker`, and a failed provision leaves the node
unprovisioned either way (no worker, no key, no `Node` document). Against that,
running the probe before `write_env` means duplicating the storage path into a
second place in the flow, and it separates the check from the moment the value
is committed to the node. Keeping them adjacent means one edit changes both.

So: `write_env` (:802) → **`check_storage`** → `install_key` (:828) →
`pull_image` (:861).

**`check_storage` is a new phase string, not a reuse.** #803 reused `write_env`
because it failed *inside* the env-writing step and had no work of its own.
`check_storage` is its own remote round trip with its own user-visible progress
message, and the issue's acceptance criteria name it explicitly. The docstring
at `nodes.py:541-543` makes phase strings a user-visible contract; a check with
its own duration deserves its own name.

**The failure message follows the #803 shape**: what was found (the token was
absent / differed at `<path>` on `<host>`) → why it is wrong (this node has its
own local directory, not the primary's data) → the remedy (mount the primary's
`BIOINFO_HOME` at `<storage_location>`, or re-provision with
`allow_unshared_storage`) → "then provision the node again."

## Decision Q3: a failed probe fails the provision, with an explicit opt-in to enrol anyway

The epic's acceptance criteria say provisioning "fails with a remedy"; the
scope boundary says a non-shared node "still enrolls" and is merely excluded
from filesystem work. Both are true statements about different users, and the
resolution is that **the default is the strict one and the permissive one is a
choice the user makes on purpose.**

- **Default: fail.** A user who typed a `storage_location` believing it was
  shared has made a mistake, and the whole point of #844 is that the mistake
  surfaces now rather than hours into an alignment. Silently enrolling a
  degraded node and hoping #845 catches it later reproduces the failure mode in
  a slower form — and #845 does not exist yet, so for one release the permissive
  default would be strictly a regression in honesty.
- **Opt-in: enrol anyway.** `ProvisionRequest` gains
  `allow_unshared_storage: bool = False`. Set true, a failed probe records
  `storage_shared = false` and provisioning continues to `install_key` and
  through to `enrolled`. This is the node that will only ever run SRA/NCBI jobs
  that fetch their own inputs — a real configuration the epic explicitly
  preserves.

The default being `False` is what makes the flag a decision. A user who sets it
has read what it means; a user who has not gets the refusal and the remedy.

**A probe that cannot run at all is a failure, not an unknown.** If the SSH
command errors, times out, or the primary cannot write its own sentinel, the
provision fails with that reason. `storage_shared = unknown` means "never
asked", not "asked and could not tell" — see Q4.

## Decision Q4: three new `Node` fields, with `storage_shared` tri-state as `bool | None`

```python
# The path this node was told to use as BIOINFO_HOME, recorded at
# provisioning. Null on nodes that enrolled themselves, and on nodes
# provisioned before this field existed.
storage_location: str | None = None

# Whether a round-trip sentinel probe proved this node reads the primary's
# BIOINFO_HOME. None means never probed -- an existing node, or one that
# enrolled itself. Distinct from False, which means probed and not shared.
storage_shared: bool | None = None

# When storage_shared was last established. Null iff storage_shared is None.
storage_checked_at: datetime | None = None
```

**`bool | None`, not a three-valued string enum.** The two known values are
genuinely boolean, `None` is Pydantic's and Mongo's native absent, and an
existing document with no such key deserialises to `None` with no migration.
A string enum would need a `"unknown"` literal *and* still handle the missing
key, giving two representations of one state.

**Why `None` must be distinguishable from `False`**:
[#846](https://github.com/syntheticgio/bioflow/issues/846) migrates existing
nodes, and its whole job is finding the ones that have never been asked. #845
excludes nodes from filesystem work; whether it should exclude `None` as well as
`False` is #845's decision to make, and it can only make it if the distinction
survives. The issue states this directly: "A node enrolled before this field
exists reads as unknown, not false."

**`storage_checked_at` is not redundant.** A probe result ages. A node that
passed six months ago and has since had its mount removed is not a node that
passed today, and #846 needs a cheap way to find stale answers. The invariant is
that it is null exactly when `storage_shared` is `None`.

**The `enumerate_nodes` hazard.** `nodes.py:98-106` reads Node fields inside a
`try/except Exception` that, per its own comment at :107-114, discards *every*
accumulated node on one bad read — this has already silently emptied two tests.
Adding `storage_shared` and `storage_location` to that dict is desirable for the
UI, and it is exactly the change the comment warns about. It is safe **only**
because the new fields have model defaults, so a `Node` deserialised from an old
document still has the attribute. It is **not** safe against a test that hands
that loop a `MagicMock`-shaped stand-in. Every existing caller's fixtures must be
checked in the same commit, and the two fields must be added together with a test
that a node document written before the field exists still enumerates.

## Decision Q5: a new endpoint, `POST /nodes/{node_id}/check-storage`

Not a parameter on `POST /nodes/provision`. Re-provisioning is a destructive
eleven-phase operation that rewrites `.env`, reinstalls the key and restarts the
worker; a user who wants to re-check a mount should not have to accept all of
that. The issue requires the probe be re-runnable "against an already-enrolled
node" — that is a different verb on a different resource.

Shape, following `update_node` (`nodes.py:999-1033`) exactly:

```
POST /nodes/{node_id}/check-storage  ->  200
```

- **404** when the node is unknown.
- **409** (`ConflictError`) when `node.ssh_key_enc is None`, with the same
  message shape `update_node` uses: the node was not provisioned from BioFlow,
  so there is no stored key to reach it with.
- **409** when `node.storage_location is None` — there is nothing to probe. This
  is the pre-#844 node that #846 will backfill.
- Connects with `node_ssh.connect_with_tofu(..., stored_host_key=node.host_key)`,
  so the pinned host key is enforced rather than re-trusted.
- Runs the same probe function `_provision_node` calls, writes
  `storage_shared` / `storage_checked_at`, and returns them.

**Synchronous, not a background task with a poll endpoint.** The probe is one
SSH connect and two short commands — seconds, not the minutes an image pull
takes. `update_node` is asynchronous because it pulls an image and drains a
worker; copying its concurrency machinery here would be machinery with nothing
to do. The timeout is bounded by the existing `_VERIFY_TIMEOUT_SECONDS`
connect timeout and a short per-command `asyncio.wait_for`.

The response body carries `node_id`, `storage_shared`, `storage_location`,
`storage_checked_at`, and a `detail` string — so a `false` result explains
itself rather than making the caller guess.

## Decision Q6: cleanup is best-effort and never fails the probe

The sentinel is removed on both paths, in a `finally`, and **a failure to remove
it is logged and otherwise ignored.**

The reasoning is that by the time cleanup runs, the probe has already answered
its question. Both the true and the false answer are established facts before the
`unlink` is attempted. Letting a failed `unlink` turn a green provision red would
mean failing a correctly-configured node over a stray 40-byte file in
`.biopipe/` — trading a real success for a cosmetic one.

Mechanically:

- The primary owns the file and deletes it with `Path.unlink(missing_ok=True)`
  in a `finally` around the whole probe, so an exception anywhere in the round
  trip still cleans up.
- Nothing is deleted on the node. The node only ever `cat`s; if the storage is
  shared, deleting on the primary removes it from the node's view too, and if it
  is not shared, there was nothing there to remove.
- A failed unlink logs `node_storage_probe_cleanup_failed` with the path.
- **Because the tokens are unique per probe, a leaked sentinel is inert** — it
  can never be mistaken for a later probe's file, and it cannot make a later
  probe pass. That is what makes best-effort cleanup safe rather than merely
  convenient.

Stray files accumulating in `.biopipe/` across many failed probes is a real if
minor cost. It is not worth a sweeper in this issue; if it becomes visible, a
prefix-and-mtime sweep at `initialize_home()` is the obvious later fix.

## The `.biopipe/lock` hazard: noted, not in scope

`lock_path` (`config.py:637-638`) is `$BIOINFO_HOME/.biopipe/lock`, and
`_acquire_lock()` takes an exclusive `fcntl.flock` on it at API startup
(`home.py:120-143`). POSIX advisory locking over SMB is unreliable — CIFS
supports it only with the right mount options, and behaviour differs by server
implementation.

**It is not in scope here, for a specific reason**: nothing in #844 mounts
anything, and the probe never touches `lock_path`. It also does not become a new
problem in #844's world, because `_acquire_lock` **already** treats
`EACCES`/`EAGAIN` as a warning rather than a hard stop, with a comment naming
FUSE unreliability as the reason.

But the failure it is guarding against is real and gets worse under #848. The
lock exists to stop two stacks racing on blob refcounts and GC. Once a node
mounts the primary's home, a node-side stack that takes the lock successfully
against a *server-local* view — or that fails to see the primary's lock at all —
gets exactly the unguarded concurrent access the lock was written to prevent, and
the current warning-and-continue posture means it happens silently.

**Flagged for #848**: that issue must decide whether the lock is enforced over
the mount (requiring a CIFS mount with working `flock`, verified rather than
assumed), or whether nodes are configured to skip `_acquire_lock` entirely
because only the primary owns the GC. The second is probably correct — a compute
node is not a second stack in the sense the lock means — but it is a decision,
not an omission, and it belongs where the mount is created.

## Requirements

- **R1.** When provisioning a node, BioFlow writes a file into the primary's
  `BIOINFO_HOME` whose content is unique to that probe run.
- **R2.** BioFlow reads that file back at the node's `storage_location` over the
  provisioning SSH connection and compares its content to what was written.
- **R3.** When the content read at the node matches what was written, BioFlow
  records `storage_shared = true` on the node's `Node` document.
- **R4.** When the file is missing at the node, or its content differs, BioFlow
  records `storage_shared = false`.
- **R5.** BioFlow removes the sentinel from the primary's `BIOINFO_HOME` after
  the probe, whether the probe succeeded or failed.
- **R6.** A failure to remove the sentinel does not change the probe's recorded
  result and does not fail the provision.
- **R7.** BioFlow reports the phase string `check_storage` through
  `NodeProvisionTask.phase` while the probe runs.
- **R8.** BioFlow runs the probe after writing the node's `.env` and before
  pulling the node's image.
- **R9.** When the probe records `storage_shared = false` and the user did not
  set `allow_unshared_storage`, BioFlow fails the provision with a message
  naming the node's `storage_location`, the primary's `BIOINFO_HOME`, and the
  action that would fix it.
- **R10.** When the user sets `allow_unshared_storage`, a `storage_shared =
  false` result does not stop provisioning and the node reaches `enrolled`.
- **R11.** When the probe cannot be carried out — the primary cannot write its
  sentinel, or the remote command errors or times out — BioFlow fails the
  provision with that reason regardless of `allow_unshared_storage`.
- **R12.** BioFlow records on the `Node` document the `storage_location` the
  node was provisioned with.
- **R13.** A `Node` document written before these fields existed reads as
  `storage_shared = None`, distinguishable from `False`.
- **R14.** A user can re-run the probe against an already-enrolled node without
  re-provisioning it, and the node's recorded `storage_shared` and
  `storage_checked_at` are updated to that run's result.
- **R15.** BioFlow refuses to re-run the probe against a node it holds no SSH
  key for, naming that as the reason.
- **R16.** `storage_checked_at` is null if and only if `storage_shared` is
  `None`.

## Testing

All new tests go in `backend/tests/api/test_node_provision.py`, which is the
established home for this flow (1127 lines) and already carries the fixtures.

**The mock gotchas, all four of which apply to every new provisioning test**
(`test_node_provision.py`):

1. `ssh.connect` must itself be an `AsyncMock`, not merely have an `AsyncMock`
   `.return_value` (:452-456).
2. `conn.close` must be a `MagicMock`, not an `AsyncMock` — #788 (:544-547).
3. `verify_key` returns a **two-tuple**; a bare `AsyncMock` dies inside the
   catch-all with no signal — #444. Use the `_verify_key_mock()` helper
   (:45-58).
4. **`_routable_primary_hostname` (autouse, :25-40) is required.** It patches
   `mod.settings.primary_hostname` to `192.168.1.50` because the suite runs in a
   container where `_primary_hostname()` refuses its own Docker address (#803).
   Without it every new provisioning test fails at :750 before reaching
   `check_storage` at all — and it fails as a `write_env` failure, which reads
   like a bug in the new code.

Also patch `_VERIFY_SETTLE_SECONDS` to 0 (:439-442). There is no shared
provisioned-`Node` fixture; nodes are built inline and `await node.delete()`d.

Concrete cases:

- **R2/R3, the happy path** — `conn.run` returns the token the primary wrote.
  Drive this by making the `cat` mock read the file the probe actually created,
  not by hardcoding a string; a test that hardcodes the expected token passes
  even if the probe compares against the wrong value. Assert `storage_shared is
  True` and `storage_checked_at` is set on the persisted `Node`.
- **R4, differing content** — `conn.run` returns `biopipe-home-v1` (the
  *existing* sentinel's content). This is the Q1 regression test and must record
  `false`. Name it so its purpose survives: the whole point of Q1 is that this
  input is what a naive implementation would accept.
- **R4, missing file** — `conn.run` returns a non-zero exit with empty stdout.
- **R9, default refusal** — with a `false` result and no
  `allow_unshared_storage`, the task ends `status == "failed"`, `phase ==
  "check_storage"`, and no `Node` document exists. Assert `"docker pull" not in
  commands` via the call-log idiom (:502-504) — that is the direct test of R8's
  ordering value.
- **R10, opt-in** — same inputs with `allow_unshared_storage=True`: the task
  reaches `enrolled`, and the `Node` has `storage_shared is False`.
- **R7** — a provisioning run passes through `phase == "check_storage"`.
  Assert it via the `_update` call sequence, not by catching a single moment.
- **R8, ordering** — assert the `cat` command appears in `conn.run.call_args_list`
  *before* the `docker pull`, by index. Ordering is the requirement; both merely
  being present does not test it.
- **R5/R6, cleanup** — after a successful probe, the sentinel path does not
  exist. Separately, patch `Path.unlink` to raise `OSError` and assert the
  provision still reaches `enrolled` and `storage_shared` is still recorded.
- **R11** — remote command raises `TimeoutError`; the provision fails even with
  `allow_unshared_storage=True`. This is the case most likely to be implemented
  as "unshared", and the test is the only thing that distinguishes them.
- **R13** — insert a `Node` with the field keys absent from the raw document and
  assert it loads with `storage_shared is None`.
- **R14/R15** — `POST /nodes/{node_id}/check-storage`: happy path flips a
  recorded `False` to `True`; 404 for an unknown node; 409 with
  `ssh_key_enc = None`; 409 with `storage_location = None`.
- **`enumerate_nodes` regression (Q4)** — `GET /nodes` returns the node when its
  document predates the new fields. This is the test that catches the :107-114
  hazard, and it must exist because the failure mode is an *empty list*, not an
  error.

**Real-data check, per CLAUDE.md.** Run the probe against the actual 5173 stack's
`BIOINFO_HOME` from a real second machine — one genuinely sharing and one with a
same-named local directory. The identically-named-local-directory case is the
one the unit tests can only simulate, and it is the entire reason #844 exists.

## Verify before implementing

1. **That `.biopipe/` is writable from the API container at probe time.**
   `initialize_home()` creates `meta_dir`, but the probe runs in a request/task
   context, not at startup. Confirm rather than assume.
2. **What `cat` of a missing file returns through `_command_output`** on a real
   node — stderr vs stdout, and the exit status. `_command_output`
   (`nodes.py:516-522`) is stderr→stdout→"no output", and R4's missing-file case
   depends on which arrives.
3. **Whether `storage_location` needs shell quoting.** It has no validator
   (`nodes.py:40-56`) and is interpolated into a remote command. `node_ssh._quote`
   (:196) exists; confirm it is applied on the probe's `cat` path. A path with a
   space is the benign case; a path with a `;` is not.
4. **Whether an SMB-mounted read is visible immediately** after the primary's
   write, or whether client-side caching delays it. If it can lag, the probe
   needs a bounded retry, and "retries once over 2 seconds" is a different
   implementation from "reads once". Check against a real CIFS mount, not by
   reasoning about the protocol.
5. **That `connect_with_tofu` with a stored `host_key` works against a node
   provisioned by the current code**, for Q5's endpoint. Every existing test
   mocks `asyncssh.connect` and would not catch a mismatch.
6. **Every existing caller's fixtures for `enumerate_nodes`** before adding the
   two field reads (Q4).

## Out of scope

- **Excluding non-shared nodes from filesystem-dependent work** — #845. This
  issue records the fact; acting on it is the next child. Nothing here changes
  job routing.
- **Creating or mounting the share** — #847 (primary-side share) and #848
  (node-side mount). The probe reports what is, and never changes it.
- **Migrating existing nodes to a recorded value** — #846. This issue only
  guarantees they read as `None` and can be probed.
- **macOS nodes.** Per the epic, Linux nodes only. The probe's node side is a
  `cat`, which is portable, but the remedy message it prints names a Linux mount
  and would mislead a Mac user.
- **The `.biopipe/lock` decision** — flagged above for #848.
- **Sweeping leaked sentinels** (Q6).
- **Validating `storage_location` as a path** at request time. Worth doing, but
  it is a change to `ProvisionRequest` that stands on its own and would make
  this diff about two things.
