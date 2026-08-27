# Node shared-storage probe — implementation plan

Issue: [#844](https://github.com/syntheticgio/bioflow/issues/844).
Design: [`docs/superpowers/specs/2026-08-25-node-storage-probe-design.md`](../specs/2026-08-25-node-storage-probe-design.md).

Line numbers are as of 2026-08-25 and will drift as earlier items land. Work the
items in order; re-locate by symbol, not by line, after item 2.

## Before the first edit

Resolve the six items in the spec's **Verify before implementing**. Two of them
change what gets written, not just whether it is right:

- **Item 2** (what a missing file returns through `_command_output`) decides
  whether the missing-file case is detected by exit status or by empty stdout.
- **Item 4** (SMB read-after-write visibility) decides whether the probe reads
  once or retries. If it must retry, that is a bounded loop with its own
  constant, and it belongs in item 1's function rather than being bolted on
  later.

Do not start item 1 with these open.

### Resolved 2026-08-27, against the live deployment

All six, checked against the running stack and the one enrolled node
(`ai-gen-desktop`, 192.168.1.237) rather than by reasoning.

1. **`.biopipe/` is writable from the API container at probe time.** Confirmed
   by writing and unlinking a file under `settings.meta_dir` (`/data/.biopipe`)
   from `biopipe-api-1` in a request-shaped context. `initialize_home()` both
   creates the directory (`storage/home.py:67`) and write-probes it at startup
   (`_assert_writable`, `:86`), so the probe is reusing an established
   guarantee rather than assuming a new one.

2. **A missing file gives exit status 1, empty stdout, and the message on
   stderr.** Verified over the real SSH connection:
   `cat: <path>: No such file or directory (os error 2)` on stderr, `''` on
   stdout, `exit_status=1`. **The probe therefore keys on exit status and
   stdout content, and must not route detection through `_command_output`**
   (`nodes.py:516-522`) -- that helper is stderr-first *for display*, so a
   missing sentinel would come back as a non-empty string and read as success.
   Item 1's comparison stays `token in result.stdout`.

3. **`storage_location` must be quoted.** `node_ssh._quote` (`:209-210`) is a
   plain POSIX single-quoter and is the right tool. Still no validator on
   `ProvisionRequest.storage_location`, so the quoting is load-bearing, not
   defensive.

4. **SMB read-after-write latency cannot be measured yet, and the probe reads
   once.** There is no CIFS mount anywhere in the deployment to measure against
   (`mount | grep -iE 'cifs|nfs|smb'` on the node returns nothing), and the
   spec is explicit that this must not be settled by reasoning about the
   protocol. So: **read once now**, and #848 -- which creates the first real
   mount -- owns re-testing this and adding a bounded retry if it proves
   necessary. Recorded there rather than left implicit here.

5. **`connect_with_tofu` works against a node provisioned by the current
   code.** Connected to `ai-gen-desktop` with its stored `ssh_key_enc` and
   `host_key`; the presented key matched the stored one and the connection was
   accepted. Worth having checked -- every existing test mocks
   `asyncssh.connect`, so nothing else exercises this path.

6. **`enumerate_nodes` fixtures** -- audited with item 8.

### What the probe found on the live deployment

Running the real nonce probe against `ai-gen-desktop`'s actual
`BIOINFO_HOME` (`/mnt/55e23b05-a79b-415d-be50-6862163c8a38`, read from its
`~/.bioflow/.env`) returns **not shared**: exit 1, no sentinel.

That node is `status: active` and has been eligible to claim `align_reads`
sub-jobs it cannot read the inputs for. This is the failure #843 was written
to remove, present in the deployment right now.

**And it confirms Q1 empirically.** The node's `.biopipe/VERSION` contains
`'biopipe-home-v1\n'` -- byte-identical to the primary's, because both ran
`initialize_home()` against their own local disk. A probe comparing the
existing sentinel would report **shared** for this node. The nonce probe
reports **not shared**. The design decision not to reuse `SENTINEL_CONTENT` is
therefore not a theoretical precaution; it is the difference between a right
and a wrong answer on the only node this deployment has.

## Changes

### 1. New probe module — `backend/app/services/node_storage_probe.py`

New file. A service module, not inline in `nodes.py`, because Q5's endpoint and
`_provision_node` both call it and `nodes.py` is already 1130 lines.

```python
SENTINEL_PREFIX = "probe-"

@dataclass
class ProbeResult:
    shared: bool
    detail: str

async def probe_shared_storage(conn, storage_location: str) -> ProbeResult
```

- Generates `token = secrets.token_hex(16)`.
- Writes `settings.meta_dir / f"{SENTINEL_PREFIX}{token}"` containing the token
  plus a human-readable line saying what the file is and that it is safe to
  delete — someone will find one after a crash.
- Runs `cat {node_ssh._quote(remote_path)}` over `conn` under
  `asyncio.wait_for`, where `remote_path = f"{storage_location}/.biopipe/{SENTINEL_PREFIX}{token}"`.
  **Quote it** — `storage_location` has no validator (`nodes.py:40-56`) and is
  interpolated into a remote command.
- Compares `result.stdout` content to `token` (substring on the first line, not
  equality on the whole file — the human-readable line is in there too).
- `finally: path.unlink(missing_ok=True)` inside its own `try/except OSError`
  that logs `node_storage_probe_cleanup_failed` and continues (spec Q6/R6).
- Raises a named `StorageProbeError` when the primary cannot write its sentinel
  or the remote command errors/times out — **distinct from returning
  `shared=False`** (R11). Returning `False` here is the single most likely
  implementation mistake and the reason the exception is a separate type.

Write the module docstring as the argument for why the existing sentinel is not
reused, citing `storage/home.py:26` `SENTINEL_CONTENT = "biopipe-home-v1\n"`.
That constant is the whole reason this file exists and a future reader will
otherwise ask why.

### 2. `backend/app/models/node.py` — three fields

Add after `host_key` (:44-47), before the `image_digest` block:

```python
storage_location: str | None = None
storage_shared: bool | None = None
storage_checked_at: datetime | None = None
```

with the comments from the spec's Q4 — specifically that `None` means *never
probed*, distinct from `False`, because #845 and #846 both depend on the
distinction and a later reader will be tempted to "simplify" it to `bool = False`.

`datetime` is already imported (:11). No `Settings.indexes` change: nothing
queries by these yet, and #846 can add one when it does.

### 3. `backend/app/api/v1/nodes.py:40-56` — `ProvisionRequest.allow_unshared_storage`

```python
allow_unshared_storage: bool = False
```

Default `False` is the decision (spec Q3), so say so in the field's comment:
without it the flag is a footgun that reads as a formality.

### 4. `backend/app/api/v1/nodes.py` — the `check_storage` phase in `_provision_node`

Insert between the `write_env` `RemoteStep` block ending ~:818 and the
`install_key` block at :821-828.

```python
await _update("check_storage", f"Checking shared storage on {req.host}…")
try:
    probe = await node_storage_probe.probe_shared_storage(
        conn, req.storage_location
    )
except node_storage_probe.StorageProbeError as e:
    task.phase = "check_storage"
    return await _fail(str(e))
if not probe.shared and not req.allow_unshared_storage:
    task.phase = "check_storage"
    return await _fail(_unshared_storage_message(req))
```

- `task.phase` is assigned **before** `_fail`, because `_fail` (:690-695) sets
  status/error/message/finished_at but not phase — the same shape as :750, :818,
  :888, :894. Always `return await _fail(...)`.
- Carry `probe.shared` forward to the `Node` write at item 5.

**In the same commit**, add a comment above the block in the register of
:821-827 and :742-751, and — load-bearing — **amend the `install_key` comment at
:821-827**. It currently reads "Before the image is pulled, so a node that
cannot take the key costs nothing." That sentence now describes two checks in
sequence, and its position as *the* pre-`pull_image` guard is no longer unique.
Left alone it does not become false, but it stops explaining the ordering a
reader is looking at. Rewrite it to name `check_storage` as the check ahead of
it and keep its own argument.

`_unshared_storage_message(req)` is a module-level helper next to
`_restarting_worker_message` (:660-667), following the #803 message shape: what
was found → why it is wrong → the remedy → "then provision the node again." It
must name `req.storage_location`, `settings.bioinfo_home`, and
`allow_unshared_storage` explicitly.

**No `NodeProvisionTask` change.** `phase` is a free-form `str` with default
`""` (`models/node_provision.py`) — verified; a new phase string needs no model
edit. Note this in the commit body so the next person does not go looking for an
enum.

### 5. `backend/app/api/v1/nodes.py` — record the fields on the `Node`

**Corrected 2026-08-27:** an earlier draft of this item said the `Node` is
written "in the `enrolled` region, ~:892-900". It is not. `_provision_node`
creates and saves the document at **:830-839**, immediately after
`node_ssh.verify_key` and *before* `pull_image` -- there is no second save
later. Writing the storage fields in an `enrolled` region would need either a
relocation or a second `save()`.

Set them on `node_doc` at :830-839, alongside the SSH fields, in the same save:

```python
node_doc.storage_location = req.storage_location
node_doc.storage_shared = probe.shared
node_doc.storage_checked_at = datetime.now(UTC)
```

This is also the natural site: `check_storage` runs before :830 (item 4), so
`probe` is already in hand when the document is built.

R16 (`storage_checked_at` null iff `storage_shared is None`) is maintained by
always writing the three together. Never write one without the others.

### 6. `backend/app/api/v1/nodes.py` — `POST /{node_id}/check-storage`

New route immediately after `update_status` (:1036-1053), before `node_status`
(:1056) — grouping it with the other per-node SSH operations rather than with
the provisioning endpoints, since that is what it is.

Model it on `update_node` (:999-1033) for the guard clauses, and **not** for the
concurrency: this is synchronous (spec Q5).

```python
@router.post("/{node_id}/check-storage")
async def check_node_storage(node_id: str) -> dict:
```

- 404 unknown node (`NotFoundError`).
- 409 `node.ssh_key_enc is None` — reuse `update_node`'s message shape verbatim
  so the two endpoints do not disagree about what that state means.
- 409 `node.storage_location is None` — nothing to probe; name #846's backfill
  as the path forward.
- `node_ssh.connect_with_tofu(node.ssh_host, node.ssh_port, node.ssh_username,
  decrypted_key, stored_host_key=node.host_key)` — **pass the stored host key**,
  so the pin is enforced. Passing `None` here would silently re-TOFU an already
  pinned node.
- `finally: conn.close()` — `close()` is synchronous (see the #788 note in the
  tests).
- Write all three fields; return `node_id`, `storage_shared`,
  `storage_location`, `storage_checked_at`, `detail`.
- On `StorageProbeError`, return an error response rather than recording
  `False` — same distinction as item 4.

Find how `ssh_key_enc` is decrypted in `node_update_service.run_update` (:46) and
reuse that call, not a second decryption path.

### 7. `backend/app/api/v1/nodes.py:98-106` — two field reads in `enumerate_nodes`

```python
"storage_shared": doc.storage_shared,
"storage_location": doc.storage_location,
```

This is the change the comment at :107-114 explicitly warns about: any error in
this loop discards **every** accumulated node, silently, with no doc id logged,
and it has already emptied two pre-existing tests. It is safe here only because
item 2 gave both fields model defaults.

**Before committing this, grep every caller and every fixture that reaches this
loop** and confirm none hands it a `MagicMock`-shaped stand-in. Do this as a
distinct step; the failure mode is `GET /nodes` returning `[]`, which no test
asserting "the response is a list" will catch.

Do **not** widen the `except` or add logging of the doc id in this commit — that
is a real improvement and a separate change (worth its own issue).

### 8. Tests — `backend/tests/api/test_node_provision.py`

Every case from the spec's **Testing** section. Non-negotiable setup for each new
provisioning test:

- `_routable_primary_hostname` (autouse :25-40) — without it the run fails at
  :750 as a `write_env` failure, which reads like a bug in the new code.
- `_verify_key_mock()` (:45-58) — `verify_key` returns a two-tuple (#444).
- `conn.close` a `MagicMock`, not `AsyncMock` (#788, :544-547).
- `ssh.connect` itself an `AsyncMock` (:452-456).
- `patch("app.api.v1.nodes._VERIFY_SETTLE_SECONDS", 0)` (:439-442).
- `pytestmark = pytest.mark.usefixtures("beanie_models")` and
  `loop_scope="module"` are module-level already (:14-20).

Two tests carry more weight than the rest and should be named for their purpose:

- **The Q1 regression** — `conn.run`'s `cat` returns `biopipe-home-v1`, and the
  result must be `False`. This is the exact input a reused-`.biopipe/VERSION`
  implementation accepts.
- **The R11 case** — a `TimeoutError` from the remote command fails the provision
  *even with* `allow_unshared_storage=True`.

Plus the `enumerate_nodes` regression: a `Node` document written without the new
keys still appears in `GET /nodes`.

### 9. Frontend — `frontend/src/api/types/system.ts` and `SettingsNodes.tsx`

**`check_storage` needs no frontend work to render.** `phaseLabel`
(`SettingsNodes.tsx:423-425`) mechanically prettifies any phase string into
"Check Storage". Verified. Do not add a phase-label map for it.

What does need changing:

1. **`system.ts:119-128`** — add `allow_unshared_storage: boolean` to
   `NodeProvisionRequest`, and `storage_shared: boolean | null` +
   `storage_location: string | null` to `NodeInfo` (:~110-118), matching item 7's
   response.
2. **`ProvisionForm` (:204-410)** — a checkbox for `allow_unshared_storage`,
   defaulting unchecked, near the storage input (:370-379) and submitted at :272.
   Label it as what it does, not as an escape hatch: it enrols a node that
   cannot read the primary's data, and the label should say that.
3. **A `Storage` column in the node table** — yes, add it. Columns are at
   :144-152. Without it, the field this whole issue exists to record is invisible,
   and #845 will start excluding nodes for a reason the user cannot see anywhere.
   Render it as the **tri-state badge already used for Status** in `NodeRow`
   (:515-521): Shared / Not shared / Unknown, each with an explanatory `title`.
   That idiom exists in this file for exactly this shape of signal; copy it
   rather than inventing a second one. Styles go with the existing `nodes-*`
   rules in `frontend/src/styles.css`.
4. **`client.ts:567-587`** — `checkNodeStorage: (nodeId: string) => request<...>(...)`
   next to `updateNode`, and wire it to a per-row action in the Actions column
   (:144-152 header, `NodeRow` body).

No `NodeProvisionStatus` change — `phase` is already `string`.

## Commits

Separable per CLAUDE.md, each independently revertable, each leaving the tree
green.

1. `feat(models): record a node's storage location and shared-storage status`
   — item 2 alone. Model fields with their comments. Nothing reads them yet.

2. `feat(api): prove a node shares the primary's storage by round trip`
   — items 1, 3, 4, 5, and the item 8 tests for them. The core of the issue.
   Body must say why the existing `.biopipe/VERSION` sentinel is not reused
   (identical content by construction) and that `NodeProvisionTask.phase` needed
   no change. `feat` and not `chore`, or it vanishes from the changelog.

3. `feat(api): re-check a node's shared storage without re-provisioning`
   — item 6 and its tests. Separable because #846 depends on this endpoint and
   nothing else in this issue does.

4. `feat(api): report each node's shared-storage status in the node list`
   — item 7 and its regression test, **alone**. Isolated on purpose: it is the
   one change that can silently empty `GET /nodes`, and a one-file revert is the
   safety net that replaces the review gate here.

5. `feat(ui): show whether a node shares the primary's storage`
   — item 9. Frontend only.

Do not fold 4 into 2. Do not fold 5 into 4.

## Verification

- `./backend/run-worktree-tests.sh tests/api/test_node_provision.py -q` — the
  worktree script, not `docker compose exec api`, which tests **main's** code.
- `./backend/run-worktree-tests.sh tests/ -q` before the PR. Items 2 and 7 touch
  a model and a shared enumeration path; a green targeted run proves less than
  it looks like it does.
- `ruff check --config backend/pyproject.toml backend/app backend/tests ops e2e`
  from the repo root — the whole tree, the way the hook does. Fix everything it
  reports, including anything pre-existing.
- `./ops/worktree-up.sh` (UI on 5273), then manually: provision against a real
  second machine that genuinely shares the home, and against one with a
  same-named local directory. **The second case is the only real test of the
  whole issue** — every unit test simulates it.
- Confirm `check_storage` renders as "Check Storage" in `ProvisionProgress`
  (:420-445) during a live provision rather than trusting the regex.
- `./ops/worktree-up.sh --down` when finished.

## Out of scope

- **#845 enforcement.** Nothing here changes job routing or claiming.
- **#847/#848 share setup and mounting.** The probe reports; it never mounts.
- **#846 migration** of existing nodes. This lands the endpoint #846 needs and
  guarantees existing nodes read `None`; the backfill itself is that issue.
- **The `.biopipe/lock` POSIX-locking-over-SMB question** — see the design's
  dedicated section. Flagged for #848, not resolved here.
- **Widening the `enumerate_nodes` catch or logging the failing doc id**
  (:107-114). A real improvement, a separate change; file it.
- **Validating `storage_location` as a path** on `ProvisionRequest`. Item 1
  quotes it for shell safety, which is what this issue needs; input validation
  is its own change.
- **macOS nodes**, per the epic.
