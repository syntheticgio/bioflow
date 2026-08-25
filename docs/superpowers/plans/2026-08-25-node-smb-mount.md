# Mount the primary's SMB share on a Linux node — implementation plan

Issue: [#848](https://github.com/syntheticgio/bioflow/issues/848)
Design: [`docs/superpowers/specs/2026-08-25-node-smb-mount-design.md`](../specs/2026-08-25-node-smb-mount-design.md)

Status: **BLOCKED on [#844](https://github.com/syntheticgio/bioflow/issues/844)
and [#847](https://github.com/syntheticgio/bioflow/issues/847).** #844 owns the
`check_storage` phase and the `storage_shared` field this plan orders the mount
against; #847 owns the share this plan mounts. Neither is a soft dependency.

Status: **Q1 and the Q9 lock change need the user's sign-off before commit 1**
— see Gate.

## Gate before touching code

Three things, in order.

1. **Q1 forecloses an option in #850.** The spec decides that the sudo
   credential is never persisted and that #850 therefore prompts at teardown
   time or prints two commands. That is a decision made *for* a downstream
   ticket. Get it confirmed, or #850 will re-open it and one of the two specs
   will be wrong.
2. **Adding sudo to provisioning is the security posture change**, not an
   implementation detail. The user has decided it; confirm the *shape* — the
   fixed seven-command set (Q3), no general helper, refuse-with-remedy when no
   sudo is available.
3. **Run the eight items in the spec's "Verify before implementing."** Items 1
   and 2 in particular can invalidate the design: if `objects/`/`staging/`/`tmp/`
   do not share a `st_dev` over CIFS, `_assert_same_filesystem`
   (`backend/app/storage/home.py:93-108`) raises `StorageUnavailableError` at
   node startup and the whole approach fails at a point no unit test reaches.
   **Do this before commit 1, not after.**

## Commits

Six commits, three PRs. Ordered so each is separately revertable and none
leaves the tree lying.

---

### PR 1 — the sudo channel (own PR)

**This should be its own PR and its own review.** It is the security surface;
it is reviewable on its own terms; and it must not be buried in a diff that also
contains mount options and fstab parsing. Merging it alone changes no behaviour
— nothing calls it until PR 2.

#### Commit 1 — `feat(api): run a fixed set of provisioning commands under sudo`

New file **`backend/app/services/node_sudo.py`**. Modelled on `node_ssh.py`:
module docstring carrying the argument, small surface, `_quote` for every
interpolation.

Contents:

- `class SudoUnavailable(Exception)` — mirrors `node_ssh.KeyInstallError`.
- `async def probe(conn) -> bool` — runs `sudo -n true`, returns whether
  NOPASSWD is available. Never passes a password.
- `async def run(conn, command: str, *, password: str | None, timeout: float)` —
  the only escalation path. Builds `sudo -S -p '' -- {command}` and passes
  `input=f"{password}\n"` when `password` is not None.
  **Verified precondition:** `asyncssh.SSHClientConnection.run` is
  `(self, *args, check=False, timeout=None, **kwargs)` and forwards `**kwargs`
  to `create_process`, whose docstring states it accepts input redirection.
  Confirmed against the installed asyncssh **2.24.0**
  (`backend/pyproject.toml:24` pins `>=2.18,<3`).
- A module-level tuple naming the seven permitted command *shapes* (spec Q3),
  and a docstring stating the discipline and its limit — it constrains shape,
  not harmlessness.
- `def validate_storage_location(path: str) -> str` — absolute path, no shell
  metacharacters, no `..`. **`ProvisionRequest.storage_location` has never had a
  validator** (`nodes.py:40-56`, GROUND.md §A) and it is interpolated into
  `mount` arguments here.

The docstring must carry the residual-risk paragraph from the spec verbatim in
substance: a compromised primary can run root commands on every node it
provisions. `crypto.py:7-11` is the house style for this — "the honest scope of
this" — and this file should read the same way.

#### Commit 2 — `feat(api): let a provisioning step supply stdin`

`backend/app/api/v1/nodes.py`:

1. **`RemoteStep` (:483-500)** — add `stdin: str | None = None`. Extend the
   docstring; it already explains why `describe_failure` takes the result rather
   than a string, and the new field needs the same treatment (why stdin exists
   at all: the password must not reach `argv`).
2. **`_execute_remote_commands` (:550-554)** — currently:
   ```python
   result = await asyncio.wait_for(
       conn.run(step.command, check=False), timeout=step.timeout
   )
   ```
   Pass `input=step.stdin` **only when `step.stdin is not None`**. Not
   `input=step.stdin` unconditionally — `input=None` and no `input` kwarg are
   not the same thing to asyncssh, and every existing step must keep its exact
   current call shape. Nine existing `RemoteStep`s depend on this
   (`nodes.py:768, 779, 789, 802, 861, 873` and the `_verify_node_operational`
   path).
3. **`_command_output` (:516-522)** — no change, but add a comment: its return
   value reaches `NodeProvisionTask.error` in Mongo, so nothing that could carry
   a credential may be interpolated into a `describe_failure`. This is R-SEC-2's
   only structural defence.

Separable from commit 1 deliberately: this touches a function every phase runs
through, and it is the one change in this ticket that could break existing
provisioning. Revertable alone.

---

### PR 2 — the mount (own PR)

#### Commit 3 — `feat(api): mount the primary's SMB share on a Linux node`

`backend/app/api/v1/nodes.py`:

1. **`ProvisionRequest` (:40-56)** — add `sudo_password: str | None = None` and
   `use_ssh_password_for_sudo: bool = False`. Extend `_check_credential`
   (:50-56): setting `use_ssh_password_for_sudo` with no `password` is a
   validation error, and setting both `sudo_password` and
   `use_ssh_password_for_sudo` is a validation error. Field docstrings must say
   the value is not persisted (R-SEC-1).

2. **New `_mount_storage_steps(...)`** returning `list[RemoteStep]`, near
   `_render_node_env` (:459). Guard-and-exit ordering per spec Q8, matching
   `ops/migrate-storage.sh`'s shape — preconditions before any mutation:
   - `command -v mount.cifs` → skip install if present
   - `findmnt -n -o SOURCE --target <loc>` → skip if already correct, **refuse**
     if a different source
   - `apt-get install -y cifs-utils`
   - credentials file via `install -o root -g root -m 600`
   - `mkdir -p <loc>`
   - `mount -t cifs ...`
   - fstab: replace the `# bioflow-managed` line for this mountpoint, or append
     one; via temp file and `mv`, never in-place

   The mount option string is a **named module constant with a comment per
   option** pointing at spec Q4. Each `describe_failure` follows the
   `UnroutablePrimaryHost` shape (`nodes.py:280-281`, message built :348-392):
   what was found → why it is wrong → exact remedy → "provision the node again."

3. **The uid/gid constant.** `uid=0,gid=0`, from a named constant, **not a
   literal**. The comment must record that this was verified — `backend/Dockerfile`
   has no `USER` directive, no compose file sets `user:`, and
   `docker exec biopipe-api-1 id` returns `uid=0(root) gid=0(root)` — and point
   at spec Q5 so a future UID change is one greppable edit.

4. **`_provision_node` (:671)** — insert the `mount_storage` phase **after the
   `install_key` block ends (:847, the `await node_doc.save()`) and before the
   `pull_image` step list (:861)**. Spec Q7: everything cheap to fail must fail
   before the 600s pull, which is the same argument the `install_key` placement
   comment already makes (:821-827).

   Follow the local conventions exactly:
   - `except RemoteCommandError as e: task.phase = e.step.phase; return await _fail(e.reason)` —
     the shape used at :832-834 and :886-888.
   - **`_fail` (:690-695) does not set `phase`.** Callers assign `task.phase`
     first (:750, :818, :888, :894). Miss this and the UI shows the previous
     phase against the new error.
   - Always `return await _fail(...)`, never a bare call.

5. **Rollback (R-848-14).** If `mount_storage` fails after the fstab write, or
   if `check_storage` fails, remove the fstab line and `umount`. Put this in a
   `try/except` around the mount block, not in the outer `finally` at :904 —
   that `finally` only does `conn.close()` and must stay that way.

6. **No `NodeProvisionTask` model change.** `phase` is a free-form `str`,
   default `""`, no enum (`models/node_provision.py`, GROUND.md §B).

7. **No `_render_node_env` / `_render_node_compose` change.** `storage_location`
   still lands in `BIOINFO_HOME` and `BIOINFO_REGISTER_ROOTS` (:459-478) and
   `BIOINFO_HOME_HOST` is still `${BIOINFO_HOME}` (:436). The mount makes those
   *true* rather than changing them. **Their paired docstring (:420-422) says
   they must be changed together — this commit changes neither, deliberately,
   and the PR description should say so** so a reviewer does not read the
   omission as an oversight.

#### Commit 4 — `feat(api): prove the mount through the node's Docker daemon`

The Q6 sibling-container check, as an extra leg of #844's `check_storage`.

- Location depends on where #844 landed the probe. If it is not already between
  `start_worker` (:873) and `verify` (:892), **move it there** — and say so in
  the PR, because it is a change to #844's code.
- Add a `RemoteStep` running
  `docker run --rm -v <loc>:/probe <backend-image> cat /probe/.biopipe/VERSION`
  and comparing to `storage.home.SENTINEL_CONTENT` (`storage/home.py:25`,
  `"biopipe-home-v1\n"`).
- Use the backend image, already fetched by `pull_image` (:861-869). This is
  why the phase sits after it.
- The failure message must name the mountpoint and explain the namespace
  problem, citing `BIOINFO_HOME_HOST` (`config.py:299-305`) and
  `variant_runner.py:213` `host_path_for` — a reader hitting this needs to know
  why an SSH-side `cat` succeeding does not settle it.

Separate commit from 3: this is the check, that is the action, and the check is
the part most likely to need adjustment once #844 is real.

---

### PR 3 — the lock, and the UI (own PR)

#### Commit 5 — `fix(api): stop a compute node claiming the shared home lock`

`backend/app/storage/home.py`:

- **`_acquire_lock` (:119-138)** — return early when
  `settings.is_compute_node` (`config.py:641-643`). A compute node has no
  business claiming exclusive use of a directory #843 exists to share, and it
  does not run GC or blob refcounting.
- **Rewrite the docstring (:120-124).** It currently promises "Refuse to run two
  stacks against one home directory." Once #848 ships, multiple stacks sharing
  one home is the *intended* configuration, and `flock` over CIFS is commonly
  locally scoped — so each node acquires "the" exclusive lock and the
  `home_lock_contended` warning (:137) never fires. The docstring must stop
  promising mutual exclusion it cannot deliver. **This is the file's central
  claim; leaving it is the difference between a documented limitation and a lie.**
- **Do not** add `nobrl` to the mount options in commit 3. It would make the
  lock silently succeed everywhere by design.

Marked `fix` and not `chore`: it changes runtime behaviour on every compute node
and belongs in the changelog.

`fix(api)` in its own commit because it is the one change here that touches code
outside the provisioning path, and the one most likely to want reverting
independently.

#### Commit 6 — `feat(ui): ask for a sudo password when adding a Linux node`

`frontend/src/components/SettingsNodes.tsx`:

- **`ProvisionForm` (:204-410)** — add the sudo password input near the
  existing storage input (:370-379) and the credential fields. Add the
  "use my SSH password for sudo" checkbox (spec Q1, R-SEC-6) — **unchecked by
  default**; the whole point is that re-purposing the SSH password is an
  explicit act. Submit at :272.
- Helper text naming NOPASSWD as the option that requires no credential at all.
- **`phaseLabel` (:423-425)** needs no change: it mechanically renders
  `mount_storage` → "Mount Storage" (GROUND.md §E).
- `frontend/src/api/types/system.ts:119-140` — add the two fields to the
  provision request type. `client.ts:567-587` needs no change if it forwards the
  body wholesale; **check, do not assume**.
- Styles are `provision-*` / `nodes-*` in `frontend/src/styles.css`.

**Do not** add a field read to the `enumerate_nodes` mongo loop
(`nodes.py:98-106`) — the comment at :107-114 records that doing so can silently
empty `mongo_nodes` for stale fixtures. Nothing here needs to.

---

## What else must change or start lying

Named explicitly, because each is a place where landing the code without the
companion edit leaves the tree self-contradictory.

1. **`storage/home.py:120-124` docstring** — commit 5. Covered above. Highest
   priority of the five: it is a promise the code stops keeping.
2. **`config.py:637-638` `lock_path`** — no docstring today. Add one pointing at
   spec Q9, or the next reader re-derives the CIFS problem from scratch.
3. **`nodes.py:406-456` / `:459-478` paired docstring (:420-422)** — states the
   two renderers must change together. Neither changes. Say so in PR 2's
   description so the omission reads as deliberate.
4. **`nodes.py:768-777` `verify_docker`** — its refuse-and-instruct posture is
   now the *exception* rather than the rule, since provisioning can escalate.
   Add a comment recording that this is deliberate: installing Docker as root is
   a much larger action than mounting a filesystem, and spec Q3's fixed set
   excludes it on purpose. Without this, the next reader sees an inconsistency
   and "fixes" it.
5. **`backend/tests/api/test_node_provision.py`** (1127 lines) — existing tests
   assert on `conn.run.call_args_list` (:502-504). Adding steps between
   `install_key` and `pull_image` changes that log. **Grep for every negative
   assertion (`assert "..." not in commands`) before assuming they still pass** —
   a `not in` assertion is exactly the kind that keeps passing while covering
   nothing.

## New issues to file

Per CLAUDE.md's file-out-of-scope-findings rule. File these; do not ask first.

- **Container runs as root** (spec Q5). `backend/Dockerfile` has no `USER`, no
  compose file sets `user:`. Cross-cutting: Dockerfile, every compose file, and
  ownership of existing `BIOINFO_HOME` trees. Pre-existing, not introduced here,
  but #848 extends its reach to another machine.
  Labels: `type:security`, `area:infrastructure`, `priority:medium`.
- **Cross-node GC coordination** (spec Q9). The home lock cannot provide mutual
  exclusion across machines sharing a CIFS home. If GC or blob refcounting ever
  needs to be safe across nodes, it needs a real mechanism — a Mongo-backed
  lease, not a filesystem lock. Epic-level, blocks nothing today because only
  the primary runs GC. Labels: `type:bug`, `area:backend`, `priority:low`.
- **`ProvisionRequest.storage_location` has no validator** — pre-existing
  (`nodes.py:40-56`). Commit 1 adds one for the sudo path; the field is also
  interpolated unquoted into `.env` (`_render_node_env:459-478`) and into
  `mkdir`/compose paths. Worth its own audit. Labels: `type:bug`, `area:api`.

## Verification

Backend tests from a worktree — **`docker compose exec api` silently tests
*main's* code**:

```bash
./backend/run-worktree-tests.sh tests/api/test_node_provision.py -q
./backend/run-worktree-tests.sh tests/storage/ tests/pipelines/test_deepvariant_paths.py -q
```

`test_deepvariant_paths.py:62-68` guards `host_path_for`'s named error for an
unset `bioinfo_home_host` — the Q6 mechanism. Run it.

Lint the whole tree the way the hook does:

```bash
ruff check --config backend/pyproject.toml backend/app backend/tests ops e2e
```

Frontend: manual at **5273** (worktree), not 5173. `./ops/worktree-up.sh` up,
`--down` when finished — a stack you brought up is yours to bring down.

**Real-hardware verification, which no test reaches:**

- **R-848-13.** Provision a node, power off the primary, reboot the node.
  It must reach a login prompt. This is the requirement `nofail` and
  `x-systemd.automount` exist for and the one whose failure mode needs console
  access to recover from. **Do this before the PR merges, not after.**
- **R-SEC-3.** During a provision, `ps auxww | grep -i sudo` on the node.
- **R-848-7.** With the share mounted, run a real DeepVariant job on the node —
  a sibling container through the Docker socket. This is the leg that fails
  silently if the namespace is wrong.
- **Spec Verify items 1-3** — `st_dev`, `flock` from two nodes, `os.rename()`
  with `chmod 0o444` (`storage/cas.py:140-144`) over a real CIFS mount.

## Out of scope for this plan

- **macOS nodes** — `mount_smbfs` and a LaunchAgent, separate implementation.
- **#847** (the share on the primary) and **#844** (the probe) — dependencies,
  not work here.
- **#850** (teardown) — constrained by spec Q1, implemented separately.
- **Generalising sudo** beyond spec Q3's seven commands, including installing
  Docker. `verify_docker`'s posture is unchanged; item 4 above only documents
  why.
- **SMB encryption in transit** (`seal`) — real throughput cost, deserves its
  own decision.
