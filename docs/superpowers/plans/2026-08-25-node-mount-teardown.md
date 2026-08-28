# Node mount teardown — implementation plan

Issue: [#850](https://github.com/syntheticgio/bioflow/issues/850)
Design: `docs/superpowers/specs/2026-08-25-node-mount-teardown-design.md`

Status: **BLOCKED on [#848](https://github.com/syntheticgio/bioflow/issues/848).**
There is nothing to tear down until #848 mounts something, and three of the
constants below (fstab marker, credentials path, `storage_location` field name)
are #848's to define.

## Gate before touching code

Confirm against merged #848 code, not against its spec:

1. `grep -n "bioflow-managed" backend/app/api/v1/nodes.py` — the fstab marker,
   character for character. A mismatch does not fail; it leaves the entry
   behind.
2. `grep -n "bioflow-smb.cred" backend/app/api/v1/nodes.py` — the credentials
   path.
3. `grep -n "storage_location" backend/app/models/node.py` — whether the field
   landed, and under what name. If #844 named it something else, use that name;
   do not add a second.
4. `grep -n "sudo" backend/app/api/v1/nodes.py` — the sudo runner's actual
   signature and how `RemoteStep.stdin` was implemented (#848 Q2 says it gained
   an optional `stdin` field). This plan reuses it rather than building a
   second sudo path.

If item 3 came back empty, change 1 below is yours; if it came back populated,
skip it.

## Changes

Numbered. Several sites must change in the same commit as their partner or the
codebase starts lying — flagged inline.

### 1. `backend/app/models/node.py` — record the mountpoint

Add `storage_location: str | None = None`. Nullable: every node provisioned
before this has no value, and Q6/R-850-13 wants "unknown" to be a distinct,
handled state rather than a guessed default.

**Same commit:** write it in `_provision_node` (`nodes.py:671`+), at the point
the `Node` document is created or updated — find it by grepping for
`ssh_key_installed_at`, which is set on the same object. A field added to the
model but never written is a field that is always `None`, and R-850-3 then never
fires on any node.

⚠️ GROUND.md §E: `enumerate_nodes` (`nodes.py:98-106`) reads node fields in a
Mongo loop and a new field read via attribute access can silently empty
`mongo_nodes` for stale fixtures — see the comment at `:107-114`. This change
does not need to touch that loop; if a later one does, use `.get()`.

### 2. `backend/app/models/node_decommission.py` — new file

`NodeDecommissionTask`, modelled on `backend/app/models/node_provision.py` (33
lines, read it first): `task_id`, `status` ("running"|"success"|"failed"),
`phase` (free-form `str`, default `""` — no enum, so new phases need no model
change), `message`, `pct`, `node_id`, `host`, `started_at`, `finished_at`,
`error`, plus `remaining: list[str]` for what was left on the node and
`remedy_commands: list[str]` for R-850-9.

Collection `node_decommissions`. Per `node_provision.py:29-33`,
`Settings.indexes` needs `IndexModel` objects, not bare strings.

**Not** a reuse of `NodeProvisionTask`: the provisioning list endpoints and the
`_clean_node_provisions` autouse fixture would start seeing teardowns.

**Same commit:** register the document wherever Beanie's model list lives —
`grep -rn "NodeProvisionTask" backend/app/db/`. An unregistered Document raises
at first use, not at import, so this fails in the first integration test rather
than at startup.

### 3. `backend/app/api/v1/nodes.py` — the teardown command set

A module-level named tuple of the four privileged commands, placed near #848's
equivalent set so a reviewer reads both together. Per Q4/R-850-14 the set is
closed:

| # | Command | Notes |
|---|---|---|
| 1 | `sudo -n true` | NOPASSWD probe; reuse #848's |
| 2 | `umount <storage_location>` | **no `-f`, no `-l`** (R-850-16) |
| 3 | fstab line removal, temp-file + `mv` | marked lines only (R-850-19) |
| 4 | `rm -f /etc/bioflow-smb.cred` | literal path, no interpolation |

Command 3 follows #848's Q8 guard 3: never an in-place edit, which can truncate
`/etc/fstab` on a full disk. Filter to a temp file, then `mv`.

Every interpolated value goes through `node_ssh._quote`
(`backend/app/services/node_ssh.py:196-198`).

Write the comment above this tuple as a design document, not a label: it is the
answer to "can BioFlow delete my data?" and the test in change 8 asserts against
it.

### 4. `backend/app/api/v1/nodes.py` — `DecommissionRequest` and the endpoint

`DecommissionRequest`: `sudo_password: str | None = None`,
`use_ssh_password_for_sudo: bool = False`. Model it on `ProvisionRequest`
(`:40-56`) including its validator style; #848's Q1 requires the SSH-password
reuse to be an explicit tick rather than an implicit fallback.

`POST /nodes/{node_id}/decommission`, mirroring `provision_node` (`:1094`+):
create a task id, `asyncio.create_task`, register in a module dict with a
`done_callback` that pops it. Plus `GET /nodes/decommission/{task_id}` mirroring
the provision-status endpoint.

`_decommission_node(task_id, node_id, req)` is the body, and **its statement
order is the requirement** (R-850-18):

```
1. find node; NotFoundError if absent
2. refuse if node.status != "revoked"            # R-850-2
3. refuse if storage_location is in the deny list # R-850-15
4. capture host/username/key/storage_location into locals
5. await node.delete(); purge Redis keys          # R-850-1, R-850-6, R-850-8
6. attempt SSH + teardown, bounded                # everything else
7. write the task's outcome
```

Steps 2 and 3 are the only refusals that precede deletion, and they precede it
because both read from the document. Put a comment on step 5 in the shape of
`nodes.py:742-751`'s: the reason it is here and not in a `finally` is that a
`finally` is one `raise` away from being reordered by a future edit, and the
symptom only appears for a user whose node is already gone.

For step 5's Redis purge, find what provisioning/enrollment wrote:
`grep -n "node" backend/app/queue/keys.py`. If nothing is node-keyed, drop the
purge and say so in the commit body rather than leaving a no-op call.

Step 6 gets `asyncssh.connect(..., connect_timeout=10)` and the whole remote
phase a cap. Catch `OSError` / `asyncssh.Error` and record unreachable as a
*normal outcome*, not an error status — the task's `status` is `"success"` with
a populated `remaining` list.

Phases, one `_update` per phase per `_execute_remote_commands`' contract
(`:545-549`, docstring `:541-543` — "a step's phase string is a user-visible
contract"): `removing_record` → `connecting` → `unmounting` → `cleaning_config`
→ `removed`. `SettingsNodes.tsx:423-425` prettifies these mechanically, so
`cleaning_config` renders "Cleaning Config" with no frontend change.

### 5. `backend/app/api/v1/nodes.py` — the guards

Before command 2: `findmnt -n -o SOURCE --target <storage_location>`.

- empty → skip the unmount (R-850-10)
- matches `//<primary>/<share>` → unmount
- anything else → skip, and append to `remaining` (R-850-20)

Before command 3: grep `/etc/fstab` for a line whose mountpoint field is the
`storage_location`.

- absent → skip (R-850-11)
- present **with** the marker → remove
- present **without** the marker → skip, append to `remaining` (R-850-19)

Match on the mountpoint field, not the whole line — #848's Q8 guard 3 makes the
same point for the write side and the read side must agree with it.

On `umount` non-zero: record the mount as still present, append to `remaining`,
and **continue to the fstab step** (R-850-17). Removing the fstab entry of a
still-mounted filesystem affects only the next boot and is correct.

### 6. `backend/app/api/v1/nodes.py` — the remedy message

When no sudo credential resolves, or the node is unreachable, build
`remedy_commands` from the node's **actual** `storage_location`. Per GROUND.md
§F, the message carries the literal command. When `storage_location` is `None`
(R-850-13), say the mountpoint is unknown rather than emitting a command with a
`None` in it.

### 7. `frontend/` — Remove and Decommission affordances

**Confirm this is wanted before building it** (spec's Verify item 6). There is
no node-removal UI today: `client.ts:567-587` has no delete, and
`SettingsNodes.tsx`'s Actions column (`:538-556`) renders only Update. Shipping
a decommission endpoint with no caller repeats exactly the gap the spec found.

If in scope:

- `frontend/src/api/client.ts` — `revoke`, `decommission`, `decommissionStatus`,
  alongside the existing node methods.
- `frontend/src/api/types/system.ts` (`:119-140`) — `NodeDecommissionStatus`,
  and `storage_location` on `NodeInfo` if the API exposes it.
- `SettingsNodes.tsx` — a Remove button in the Actions column; a confirmation
  dialog modelled on the existing update-confirmation dialog (just below
  `NodeRow`); a progress view modelled on `ProvisionProgress` (`:420-445`)
  polling at 3000ms like `provisionStatus` (`:24-33`); a result view modelled on
  `ProvisionResult` (`:460-480`) that renders `remaining` and
  `remedy_commands` prominently. A teardown that left an fstab entry behind must
  not read as a clean removal.
- `frontend/src/styles.css` — reuse the `provision-*` / `nodes-*` families.

### 8. `backend/tests/api/test_node_decommission.py` — new file

Module setup copies `backend/tests/api/test_node_provision.py:14-20`:

```python
pytestmark = pytest.mark.usefixtures("beanie_models")
asyncio_module_loop = pytest.mark.asyncio(loop_scope="module")
```

Both autouse fixtures from that file are needed: `_routable_primary_hostname`
(`:25-40`, without it `_primary_hostname()` refuses the container's own address
per #803) and an equivalent `_clean_node_decommissions`. Mock pattern A
(`:444-458`) with the three gotchas — `ssh.connect` an `AsyncMock` itself,
`conn.close` a `MagicMock` (#788), `_verify_key_mock()` for the two-tuple
(#444). Nodes built inline, `await node.delete()`d.

Tests per the spec's Testing section. The two that carry the most weight:

- **R-850-18 ordering.** A connect mock and a deletion spy that both record a
  monotonic timestamp; assert deletion is first. An end-state-only assertion
  passes for a `finally`-based implementation, which is the one being ruled out.
- **R-850-14 source-level.** Assert over the change-3 tuple that no entry
  contains `rm -r`, `find`, `-delete`, `shred` or `mkfs`, and that the single
  `rm -f` entry's path is the literal `/etc/bioflow-smb.cred`. Not a behaviour
  test — it must survive a refactor that a behaviour test would not.

Absence assertions use the call-log pattern (`test_node_provision.py:502-504`):

```python
commands = " ".join(str(c) for c in conn.run.call_args_list)
assert "umount" not in commands
```

## Commits

Conventional Commits per CLAUDE.md; separable, so one can be reverted without
unpicking three. `feat`/`fix` reach the changelog, `test`/`docs` do not.

1. `feat(api): record a node's storage location on the node document` —
   change 1. Body: teardown must know what to unmount without connecting to the
   node, which is the case it exists for. Skip this commit entirely if #844 or
   #848 already landed the field.
2. `feat(models): add a decommission task for node teardown` — change 2.
3. `feat(api): tear down a removed node's SMB mount` — changes 3, 4, 5, 6.
   The core. Body must carry: why the record is deleted before SSH is attempted,
   why the command set is closed and contains nothing that removes a path under
   the mountpoint, and the honest cost — a removal without a sudo credential
   leaves a mount and an fstab entry behind.
4. `test(api): cover node decommissioning, reachable and not` — change 8.
5. `feat(ui): remove a node, and say what was left on it` — change 7, if in
   scope. Separate because it is independently revertible and because the
   backend is useful without it.

The mechanical rename in commit 1 and the behaviour change in commit 3 stay
apart deliberately.

## PR

Title lands in the release notes verbatim; label `type:feature` +
`area:backend` (+ `area:frontend` if change 7 lands) so `.github/release.yml`
does not file it under "Other changes". `Closes #850`.

The description must carry the two things the diff cannot say:

- **The finding.** Revocation does nothing today — `revoke_node`
  (`nodes.py:1073-1086`) flips a flag and nothing else, and it has no frontend
  caller. This PR does not fix that; it builds the first remote-teardown path
  and scopes itself to the mount.
- **The accepted cost.** BioFlow cannot always finish what it started. Per
  #848's Q1 the sudo credential is not retained, so a decommission without a
  fresh credential prints instructions instead of running commands.

## Verification

- `./backend/run-worktree-tests.sh tests/api/test_node_decommission.py tests/api/test_node_provision.py -q`
  — the provisioning suite too: change 1 touches `_provision_node`.
- `ruff check --config backend/pyproject.toml backend/app backend/tests ops e2e`
  from the repo root. Not a CI check; the run that reports `E501` also reports
  `F821`.
- UI: `./ops/worktree-up.sh`, exercise at **5273** (not 5173). Bring it down
  with `--down` when finished.
- **Real hardware, not unit-testable.** Provision a real Linux node per #848,
  decommission it, confirm `findmnt` reports nothing at the mountpoint and
  `/etc/fstab` has no marked line — then **reboot it** and confirm it boots
  normally and does not mount the share.
- **Unreachable path on real hardware:** power the node off, decommission,
  confirm the record is gone and the message names the host.

## Out of scope

Per the spec's Out of scope, plus two things to **file as issues** rather than
build:

- Decommission should also stop the worker, remove `~/.bioflow`, and strip the
  `authorized_keys` line — the broader gap this ticket's investigation found.
  File it referencing the machinery built here.
- `POST /nodes/decommission/manual` for finishing teardown on a node BioFlow has
  already forgotten.

Both are pre-authorized to file per CLAUDE.md, and both should be filed even if
neither is scheduled.
