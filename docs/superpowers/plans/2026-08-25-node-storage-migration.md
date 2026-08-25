# Migrating existing nodes onto recorded storage status — implementation plan

Issue: [#846](https://github.com/syntheticgio/bioflow/issues/846)
Spec: `docs/superpowers/specs/2026-08-25-node-storage-migration-design.md`

## Do not start until

1. **[#844](https://github.com/syntheticgio/bioflow/issues/844) is merged**, and
   its five surface questions from the spec's "Verify before implementing" are
   answered — in particular item 1 (is the probe a service function taking an
   open connection, or only a route handler?). That answer decides the shape of
   change 1 below and there is no useful fallback.
2. Nothing else. This child depends on #844 alone. It **must not** wait for
   #845 — the ordering runs the other way (spec Q5), and this merging first is
   what stops #845 from breaking a working deployment.

## Commit 1 — `feat(api): sweep every node's shared-storage status on demand`

Backend only. One commit because the sweep, its outcome classification and its
report are one behaviour; splitting them leaves a sweep that records nothing or
a report of nothing.

1. **Reuse #844's probe.** #844's spec (Decision Q5) says
   `POST /nodes/{node_id}/check-storage` "runs the same probe function
   `_provision_node` calls" — call **that function**, not the HTTP endpoint. Its
   two 409s (`ssh_key_enc is None`, `storage_location is None`) are route-level
   guards; the sweep needs those as *classifications*, not exceptions, so it
   checks both preconditions itself before dispatching. If the probe did not in
   fact land as a reusable function, extract it **in this commit** — a second
   implementation is not an option.

2. **New `storage_check_service.py` under `backend/app/services/`**, modelled
   on `node_update_service.py:84-112`, which already does the reconnect this
   needs:
   - Two preconditions checked **before** any connection, each a classification
     rather than an error: `ssh_key_enc is None` is the **self-enrolled**
     outcome (R4), and `storage_location is None` is the **no recorded path**
     outcome (R7a) — the latter being the common case for every pre-#844 node,
     since #844 writes that field only at provision time.
   - `crypto.decrypt(node.ssh_key_enc)` after those pass.
   - `connect_with_tofu(host, port, username, pem, stored_host_key=node.host_key,
     timeout_seconds=20)`, the same call and the same timeout.
   - **Five** outcomes exactly as the spec's table: `shared` / `not_shared` /
     `unreachable` / `not_probeable` / `no_recorded_path`. **Only `shared` and
     `not_shared` write `storage_shared` or `storage_checked_at`**; the other
     three leave both untouched, including when `storage_shared` is already
     `True` (R4/R5/R7a/R10).
   - An optional `storage_locations: dict[str, str]` argument (spec Q2a). For a
     node with a null path, a supplied value is **written to the `Node` before**
     the probe runs; an absent one classifies as `no_recorded_path`. **Never
     fall back to `ProvisionRequest`'s `/data/scratch` default** — it is a form
     default, and probing the wrong directory records a confident wrong `false`.
   - **Per-node try/except and per-node connection close.** One node's SSH
     timeout must not end the sweep, and a `finally` at sweep scope rather than
     node scope leaks connections. Both are the mixed-sweep test's subject.
   - `conn.close()` is **not** awaited — `#788`, recorded in
     `tests/api/test_node_provision.py:544-547`.

3. **`POST /nodes/storage-check` in `backend/app/api/v1/nodes.py`**,
   **synchronous**, following #844's `check-storage` rather than `update_node`'s
   task-and-poll — #844 made the per-node probe synchronous because copying that
   machinery would be "machinery with nothing to do", and this is N of those in
   sequence. Confirm the node count fits an ordinary request first (spec
   pre-implementation item 4); if it does not, fall back to the task shape and
   say so in the PR.
   - Body: optional `storage_locations` map (Q2a).
   - Response: per node, its id, outcome, and for `not_shared` the remedy
     naming **that node's** `storage_location` (R7); for `no_recorded_path`,
     the ask (R7a).

4. **New `backend/tests/api/test_node_storage_check.py`.** Every test in the
   spec's Testing section. Module setup copied from
   `tests/api/test_node_provision.py:14-20` — `pytestmark =
   pytest.mark.usefixtures("beanie_models")`, `loop_scope="module"` matching it,
   and **the autouse `_routable_primary_hostname` patch (`:25-40`)**, which any
   new test in this area needs or `_primary_hostname()` refuses the container's
   own Docker address (#803). No shared provisioned-Node fixture exists; build
   nodes inline and `await node.delete()`, as the existing file does.

   Write **R7a and R6 first**. R7a asserts the probe was **not called** for a
   node with no recorded path — not merely that the record is unchanged, which a
   wrongly-probed-and-missed node would also satisfy. R6 — the node with a long successful job history whose probe
   does not match must record `false`. It is Q1 as a test and it fails loudly
   against any implementation that took the shortcut this child exists to
   refuse.

## Commit 2 — `feat(ops): check every node's shared storage from a shell`

5. **New `ops/check-node-storage.sh`.** Thin by construction: POST, poll, format
   the four outcomes. Conventions per GROUND.md section F, none of them optional:
   - `#!/usr/bin/env bash`, then `set -euo pipefail`.
   - Header comment as a **design document** linking to
     `docs/superpowers/specs/2026-08-25-node-storage-migration-design.md`, with
     a `Usage:` line.
   - Positional args then `for arg` + `case`; no getopts.
   - **Guard-and-exit preconditions before anything else** — stack running, API
     reachable — each with the literal remedy command, `echo ... >&2; exit N`.
     No `die()` helper; the repo does not have one.
   - Ends by printing the next command — for a sweep that found not-shared
     nodes, that is the remedy; for a clean sweep, nothing further.
6. **New `ops/tests/test_check_node_storage.py`** — every `ops/*.sh` ships with
   one. Cover the preconditions exiting non-zero with a remedy on stderr, which
   is what the header actually promises.

## Commit 3 — `feat(ui): re-check a node's storage from the nodes table`

Only if #845's UI work has not already landed the column. **Check before
writing.** If #845 merged first — it should not have (spec Q5) — this commit is
the row control alone against its existing column.

7. **`frontend/src/components/SettingsNodes.tsx`** — a control that invokes the
   sweep and polls it, on the 3000ms pattern at `:24-33`.
8. **`frontend/src/api/client.ts`** — the two calls, alongside the nodes calls
   at `:567-587`.

## Verification

- `./backend/run-worktree-tests.sh tests/api/test_node_storage_check.py tests/api/test_node_provision.py -q`
  — from the worktree; `docker compose exec api` silently tests main's code.
- `./backend/run-worktree-tests.sh tests/ -q` before the PR.
- `/usr/local/bin/python3.12 -m pytest ops/tests/test_check_node_storage.py -q`
  — `python`/`python3` resolve to a venv without the app's dependencies.
- `ruff check --config backend/pyproject.toml backend/app backend/tests ops e2e`
  from the repo root, whole tree.
- **Against the real deployment, before #845 merges.** Run the sweep on the
  maintainer's actual nodes and confirm each answer independently. Every test
  above proves the sweep records what the probe said; none proves the probe was
  right about a real machine, and #845 is about to trust these values. **A
  `true` that is wrong is worse than no migration at all** — that is the failure
  the whole epic exists to remove, and this is the last point at which a human
  can catch it.
- **Confirm no sentinels are left behind** in `BIOINFO_HOME` after a sweep over
  N nodes (spec pre-implementation item 5).
- **Expect the first real run to migrate nothing** and to report every node as
  *no recorded path*. That is correct, not a bug: #844 writes
  `storage_location` only at provision time. The second run, with the paths
  supplied, is the one that migrates.

## What must change in the same commit or start lying

- The four-outcome classification, the fields written for each, and the report
  wording are one decision. A commit that adds an outcome without its write rule
  records a state nothing acts on.
- `ops/check-node-storage.sh`'s header `Usage:` line versus its actual argument
  parsing.

## After the merge

- **File the periodic re-verification issue** as a child of #843, per the
  spec's Q3 and CLAUDE.md's out-of-scope-issues rule: 21600s,
  `JobClass.MAINTENANCE`, `catchup=False`, registered in
  `scheduler.DEFAULT_SCHEDULES` (`scheduler.py:22`), never marking an
  unreachable node `false`. The decision is made and recorded; only the
  implementation is deferred.
- **Then unblock #845.** Its merge is safe once the deployment's nodes carry
  real values.

## Out of scope

- The probe (#844), enforcement (#845), the re-verification schedule (its own
  child), share setup (#847/#848), giving self-enrolled nodes an SSH path
  (that is re-provisioning), and backfilling `Node.storage_location` (raise it
  on #844 rather than inferring a path here).
