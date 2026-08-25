# Migrating existing nodes onto recorded storage status — design

Date: 2026-08-25.

Closes [#846](https://github.com/syntheticgio/bioflow/issues/846). Child 3 of
[#843](https://github.com/syntheticgio/bioflow/issues/843).

**Depends on [#844](https://github.com/syntheticgio/bioflow/issues/844)** — it
reuses #844's probe wholesale and adds no verification of its own.
**Ships before [#845](https://github.com/syntheticgio/bioflow/issues/845)**;
see Decision Q5.

## Why this child is not optional

The maintainer's deployment has nodes enrolled and running filesystem-dependent
work correctly, against shared storage configured by hand before BioFlow knew
what shared storage was. #844 adds `storage_shared`; those nodes read `None`.
#845 treats `None` as not-shared, which is the correct safe default and, applied
to that deployment, stops work that has been running correctly for months.

The recoverable part is that the answer is not unknown — it is merely unrecorded.
A probe can establish it. This child is the difference between a safe default
that costs a deployment its capacity and one that costs it a single run of a
script.

## What exists today

Verified against this worktree on 2026-08-25.

- **`Node` has no storage fields before #844** (`models/node.py`, 57 lines;
  GROUND.md section B). `storage_location` is written into the node's `.env` by
  `_render_node_env` (`nodes.py:459-478`) and then forgotten — the primary keeps
  no record of it.
- **`Node.ssh_*` is NULL for self-enrolled nodes.** A node that enrolled via
  `POST /nodes/enroll` (`nodes.py:920-978`, which takes a raw dict) has no
  `ssh_host`, `ssh_username` or `ssh_key_enc`. Only nodes created through
  `_provision_node` get an SSH key installed (`nodes.py:828-843`,
  `node_doc.ssh_key_enc = crypto.encrypt(private_pem)`). `enumerate_nodes`
  already surfaces this as `"updatable": doc.ssh_key_enc is not None`
  (`nodes.py:105`) — **the existing name for "we cannot reach this node over
  SSH".**
- **#844's probe is `POST /nodes/{node_id}/check-storage`, synchronous** (its
  Decision Q5): one SSH connect and two short commands, seconds not minutes,
  returning `node_id`, `storage_shared`, `storage_location`,
  `storage_checked_at` and a `detail` string. It **409s** when
  `node.ssh_key_enc is None` and — the case that matters here — **409s when
  `node.storage_location is None`**, which its own text names as "the pre-#844
  node that #846 will backfill."
- **`Node.storage_location` is written only at provision time** (#844's R12).
  A node provisioned before #844 has `storage_shared = None` *and*
  `storage_location = None`. The primary never recorded the path the user
  typed (`nodes.py:459-478` puts it in the node's `.env` and forgets it).
- **There is an established fan-out-over-stored-credentials pattern.**
  `node_update_service.py:84-112` decrypts `ssh_key_enc`, refuses with a
  remedy when it is absent or the host/username is missing, and reconnects via
  `connect_with_tofu(..., stored_host_key=node.host_key)`. Its API shape is
  `POST /nodes/{node_id}/update` (`nodes.py:999`) returning a task id, with
  `GET /nodes/update/{task_id}` (`:1036`) polled for progress — the same shape
  as provisioning.
- **`ops/` conventions** (GROUND.md section F): `set -euo pipefail`, a header
  comment that is a design document linking to the spec, guard-and-exit
  preconditions before any mutation, errors to stderr with the literal remedy
  command, ends by printing the next command, and a matching `ops/tests/test_*.py`.
  No `ops/*.sh` currently reaches into Mongo or the API; the closest model,
  `migrate-storage.sh`, operates on `.env` and `docker compose` only.
- **`scheduler.py:22-80`** — `DEFAULT_SCHEDULES`, seeded on first startup and
  editable at runtime through `/schedules`. Existing entries range from 60s
  (`verify_files`) to 21600s. `catchup=False` throughout, because "the Docker VM
  pauses when the laptop lid closes, so a four-hour sleep must produce one tick
  on resume, not 240" (`:8-10`).

## Decision Q1: probe every node; infer nothing from the fact that it works

**No node is marked `storage_shared = true` on any basis other than #844's
round trip returning a match.**

The tempting shortcut — a node that has been completing `align_reads` must be
able to read the primary's storage, so mark it shared — is the exact failure
#843 exists to remove, arrived at from the other direction. A node whose work
history happens to contain only self-fetching jobs (`download_sra_run`,
`download_assembly`) would be blessed as shared on evidence that says nothing
about the question. The result is worse than the state before this epic: a
recorded `true` that #845 trusts, on a node with a private `/data`, failing
hours into the first chunked alignment that reaches it — with the nodes table
asserting it should not have.

The same applies to weaker inferences: matching path strings, a `mount` table
containing a CIFS entry, or a successful `stat` of the node's `storage_location`.
#844's scope boundary already rules each of these out, for the same reason —
they all pass for a node with an identically-named local directory.

So there is exactly one way a node gets `true`, and this child does not own it.

## Decision Q2: an API endpoint that fans out, plus a thin `ops/` wrapper

**`POST /nodes/storage-check` sweeps every node, reusing #844's per-node probe.
`ops/check-node-storage.sh` calls it, for the operator who is at a shell and not
in the UI.**

Rejected alternatives, and why:

- **A standalone `ops/` script owning the logic.** No `ops/*.sh` reaches into
  Mongo or decrypts `ssh_key_enc`, and this one would have to do both. It would
  need Beanie initialisation, the Fernet key, and `connect_with_tofu` — i.e. it
  would run inside the `api` container anyway and would duplicate
  `node_update_service`'s connect-and-refuse logic in bash. The rule this
  violates is not stylistic: a second implementation of "reach a node over its
  stored credentials" drifts from the first, and the drift is invisible until a
  node fails to be reached by one path and not the other.
- **A startup hook.** Provisioning takes minutes and SSH connections time out.
  Doing this on API startup makes boot time a function of how many nodes are
  powered off, and a failure during boot has nowhere to report to.
- **A scheduled job as the migration mechanism.** Wrong tool for a one-shot.
  Its results would land in a job's output rather than in front of the person
  who needs to read the not-shared remedies. (A schedule for *re-verification*
  is a separate question — Q3.)

**Synchronous, following #844's probe rather than `update_node`'s task shape.**
#844's Decision Q5 made the per-node probe synchronous because it is one connect
and two short commands, and copying `update_node`'s poll machinery would be
"machinery with nothing to do." The sweep is N of those in sequence. At the
single-digit node counts this tool targets, N × (a few seconds, or a 20s connect
timeout for an offline node) stays inside an ordinary request. If the deployment
is large enough that it does not — check before implementing — the fallback is
the `update_node` task-and-poll shape, not a partial sweep that returns early.

**Each node lands in exactly one of four outcomes**, and the classification is
the substance of this child:

| Precondition | Outcome | `storage_shared` | Reported as |
|---|---|---|---|
| `ssh_key_enc` is None (#844 409) | not probeable | left `None` | *Cannot check — this node enrolled itself and BioFlow holds no SSH key for it.* |
| `storage_location` is None (#844 409) | no recorded path | left `None` | *Cannot check — BioFlow has no record of where this node's storage is. Supply the path, or re-provision.* |
| SSH connect fails | unreachable | left `None` | *Cannot check — {host} did not answer. The machine may be off.* |
| Probe: content matches | shared | `True` | *Shared.* |
| Probe: missing or differs | not shared | `False` | *Not shared — remedy.* |

**The `storage_location is None` outcome is the common case, not a corner**, and
it is the one this child exists for. #844 writes `storage_location` only at
provision time (its R12), so *every* node in the maintainer's deployment has a
null path and #844's probe refuses all of them with a 409. A sweep that merely
called the endpoint per node would report five 409s and migrate nothing. See
Decision Q2a.

**A node that is not probeable and a node that is unreachable are both left
`None`, and both are reported.** Never `False`. `False` is a positive claim that
the probe ran and disagreed; asserting it because a machine was powered off
would be the same category of lie as Q1's, and under #845 it is behaviourally
identical to `None` anyway — so there is nothing to gain and a false record to
lose.

**Self-enrolled nodes are a real case, not an edge case**, and the report must
distinguish them from merely-offline ones because the remedies differ. An
offline node needs powering on and a re-run; a self-enrolled node can never be
probed this way at all and needs #844's probe run from the node side, or
re-provisioning. `enumerate_nodes` already computes exactly this distinction as
`updatable` (`nodes.py:105`); reuse that fact rather than deriving a second one.

### Q2a: the operator supplies the missing `storage_location`, once, per node

A node with no recorded path cannot be probed and the path cannot be inferred —
inferring it is the same class of guess Q1 forbids, and the wrong path probes
the wrong directory and answers confidently.

So the sweep takes an **optional per-node path map**, and its report tells the
operator exactly which nodes need one:

```
POST /nodes/storage-check  {"storage_locations": {"node-a": "/data/scratch"}}
```

For each node with a null `storage_location`: if the map supplies one, write it
to the `Node` **before** calling the probe (which then finds it and proceeds);
if it does not, classify as *no recorded path* and report it with the remedy.
The operator's second run supplies the paths the first run asked for. Two runs
is the honest cost of a fact the system genuinely does not hold.

The default `ProvisionRequest.storage_location` is `"/data/scratch"`
(`nodes.py:40-56`), and it is tempting to fall back to it. **Do not.** It is a
form default, not a record of what any particular node was given, and a node
provisioned with a different path would be probed at `/data/scratch`, find
nothing, and be recorded `false` — a wrong `false` that looks like a real
answer. Ask rather than assume; a reported *cannot check* is honest and a wrong
`false` is not.

The `ops/` wrapper follows GROUND.md section F: `set -euo pipefail`, header
comment linking to this spec, preconditions checked before anything (stack
running, API reachable) with the literal remedy command on stderr, and it ends
by printing the next command. It is thin by construction — it POSTs, polls, and
formats the four outcomes. It ships with `ops/tests/test_check_node_storage.py`,
as every `ops/*.sh` does.

## Decision Q3: yes to periodic re-verification, at 6 hours, in a later child

A share can be unmounted after enrollment — an SMB mount that does not survive a
node reboot is the ordinary failure, not an exotic one, and it produces exactly
the state this epic exists to remove: a recorded `storage_shared = true` that
#845 trusts and that is no longer true. **A fact established once and never
re-checked decays into the lie it replaced.** So the answer is yes.

**But not in this child**, and the reason is scope-shaped rather than
enthusiasm-shaped. This child is a one-shot migration whose whole risk profile
is "runs once, on demand, with a person reading the output." A schedule has a
different one: it runs unattended, it SSHes into every node every interval, and
a bug in it flips `storage_shared` on nodes nobody was looking at. Bundling them
means the migration cannot merge until the schedule is right.

The recommendation, recorded here so the decision is not re-litigated:

- **Interval 21600s (6 hours)**, matching `sweep_storage_drift`
  (`scheduler.py:72`) — the existing entry that asks the same class of question
  about the primary's own storage.
- **`JobClass.MAINTENANCE`**, and `catchup=False` like every other entry
  (`scheduler.py:8-10`).
- **Registered in `DEFAULT_SCHEDULES`** (`scheduler.py:22`), so it is editable
  at runtime through `/schedules` and a user who does not want it can set the
  interval or disable it without a code change.
- **It may flip `true` → `false`, and must never flip `false` → `true` silently
  in a way the user does not see.** A share that vanished is news; the nodes
  table's badge and a system event are the reporting surface.
- **A sweep must never mark an unreachable node `false`.** Same rule as Q2, and
  more load-bearing here: an unattended sweep during an overnight reboot would
  otherwise mark the whole cluster not-shared.

File it as a child of #843 when this merges, per CLAUDE.md's out-of-scope-issues
rule, and link it from this spec's Out of scope.

## Decision Q4: idempotent because it is stateless, and re-runnable because it must be

The sweep holds no cursor and no "already migrated" marker. Every run probes
every node it can reach and overwrites that node's three storage fields with
what it just observed. Running it twice produces the same records as running it
once, and running it after a node's mount changes produces the *correct* records
rather than the first run's.

Two consequences worth naming:

- **It is not "migrate the ones that are `None`."** Scoping it to unrecorded
  nodes would make the second run a no-op on a node whose share was unmounted
  between runs — which is the case where re-running is the whole point. This is
  also what makes the same endpoint serve as Q3's mechanism later, rather than
  needing a second one.
- **`storage_checked_at` is written on every probe that ran**, including one
  that returns `false`, so "when did we last actually ask?" is answerable. It is
  **not** written for a node that was unreachable or not probeable — nothing was
  checked, and a timestamp there would report a check that never happened.

#844 owns the sentinel's cleanup on both success and failure; this child inherits
that and adds nothing. The one thing it must not do is leave a sentinel behind
per node per sweep — verify against #844's implementation rather than assuming.

## Decision Q5: this merges before #845

Both orders were considered against the state of the system in between.

- **#845 first.** Every pre-existing node reads `None`, #845 treats `None` as
  not-shared, and the maintainer's deployment stops running filesystem-dependent
  jobs on nodes that were running them correctly. The window lasts until #846
  merges and someone runs it. The breakage is *correct* by #845's rules and
  entirely avoidable, which is the worst combination: a user experiences a
  regression that the design intended not to cause.
- **#846 first.** It depends only on #844. In the window before #845, nothing
  enforces anything — storage status is recorded and unread, exactly the state
  #844 alone leaves the system in. No behaviour changes. Then #845 lands into a
  deployment whose nodes already carry the right values, and the safe default
  applies only to nodes that genuinely have not been checked.

**#846 first**, therefore. The asymmetry is that this child is a no-op when
there is nothing to migrate and #845 is a regression when there is.

This is a **hard ordering constraint on the merge sequence**, not a preference,
and it belongs in both PR descriptions. #845's spec records the same conclusion
from the other side (its Decision Q3).

## Requirements

- **R1.** A maintainer can run one operation that probes every enrolled node's
  storage without re-provisioning any of them.
- **R2.** A node whose probe matches is recorded `storage_shared = true`.
- **R3.** A node whose probe does not match is recorded `storage_shared = false`.
- **R4.** A node that BioFlow holds no SSH key for is left `storage_shared`
  unchanged and reported as not checkable, distinctly from an unreachable node.
- **R5.** A node that cannot be reached over SSH is left `storage_shared`
  unchanged and reported as unreachable.
- **R6.** No node is recorded `storage_shared = true` on any evidence other
  than the probe returning a match.
- **R7.** A maintainer reading the result sees, for each node recorded
  not-shared, one remedy naming that node's `storage_location`.
- **R7a.** A node with no recorded `storage_location` is left
  `storage_shared` unchanged and reported as needing one, never probed at a
  guessed path.
- **R7b.** When the maintainer supplies a `storage_location` for such a node,
  BioFlow records it and probes that node at that path.
- **R8.** Running the operation twice in succession leaves the same records as
  running it once.
- **R9.** Running the operation after a node's storage changes records the new
  answer, not the previously recorded one.
- **R10.** `storage_checked_at` is written for every node whose probe ran and
  for no node whose probe did not.

## Testing

- **R2/R3** — patch #844's probe to match and to differ; assert the recorded
  value in each case. Assert on the `Node` document after the sweep, not on the
  sweep's return value: the report being right while the write did not happen is
  a real failure mode and a return-value assertion misses it entirely.
- **R7a, and write it early** — a node with `storage_location = None` and no
  supplied path is reported, not probed. Assert the probe was **not called**,
  not merely that the record is unchanged: a probe called at `/data/scratch`
  that happened to miss also leaves `storage_shared` alone on a `false` write
  bug, and would pass a record-only assertion.
- **R7b** — the same node with a supplied path records that path and probes it.
- **R4** — a node with `ssh_key_enc = None`, the self-enrolled case. Assert
  `storage_shared` is **unchanged** (both from `None` and from a pre-set `True`,
  so the test catches a blanket reset) and that its report line is distinct from
  R5's.
- **R5** — patch `connect_with_tofu` to raise `asyncssh.Error`; same two
  assertions.
- **R6, as an explicit negative** — a node with a long successful job history
  and a probe that does not match records `false`. This is Q1 written as a test
  and it is the one to write first: it fails loudly against any implementation
  that took the shortcut.
- **R8** — run the sweep twice against unchanged mocks, assert identical
  documents. **R9** — run it twice with the probe flipping between, assert the
  second answer wins.
- **R10** — assert `storage_checked_at` moves on a probed node and is untouched
  on an unreachable one.
- **Mixed sweep** — one node of each of the **five** outcomes in a single run,
  and assert the sweep reports all five. Two independent failures here: one node's
  SSH timeout aborting the whole sweep, and a `finally` that runs per-sweep
  rather than per-node. Neither shows up in a single-node test.
- **Test conventions** (GROUND.md section D): `pytestmark =
  pytest.mark.usefixtures("beanie_models")`, `loop_scope="module"` matching it,
  and the autouse `_routable_primary_hostname` patch — **any new test needs it**,
  because tests run in a container where `_primary_hostname()` refuses its own
  Docker address (#803). Mock gotchas: `ssh.connect` must itself be `AsyncMock`;
  `conn.close` must be `MagicMock`, not `AsyncMock` (#788); `verify_key` returns
  a two-tuple and a bare `AsyncMock` dies inside the catch-all with no signal
  (#444) — use the `_verify_key_mock()` helper at
  `tests/api/test_node_provision.py:45-58`.
- **`ops/tests/test_check_node_storage.py`** — as every `ops/*.sh` ships with.
  Cover the preconditions (stack down, API unreachable) exiting non-zero with a
  remedy on stderr, which is what the header's contract actually promises.
- **Against the real deployment, not fixtures** (CLAUDE.md) — run the sweep on
  the maintainer's actual nodes and confirm each answer independently before
  #845 merges and starts trusting them. This is the check that matters most:
  every test above proves the sweep records what the probe said, and none of
  them proves the probe was right about a real machine.

## Verify before implementing

1. **#844's probe surface as merged** — whether it is a service function that
   takes an open `asyncssh` connection or a route handler that opens its own.
   The sweep should reuse the former; if #844 shipped only the latter, factor
   the connection-taking half out **as part of this child** rather than opening
   a second connection per node or duplicating the probe.
2. **Whether #844's probe writes the `Node` fields itself or returns a result
   for the caller to write.** Decides whether the sweep writes at all, and
   whether R10's "not written when the probe did not run" is this child's
   responsibility or already satisfied.
3. **How many nodes the deployment actually has**, and therefore whether the
   sweep runs nodes sequentially or concurrently. Sequential is simpler and
   fine at single digits; a 20s connect timeout (`node_update_service.py:105`)
   times N is the number to check against the frontend's polling patience.
4. **Whether the sweep stays synchronous.** Count the deployment's nodes and
   multiply by #844's connect timeout for the offline case. If it does not fit
   an ordinary request, fall back to `update_node`'s task-and-poll shape
   (`nodes.py:999-1035`) and decide the task collection then —
   `NodeProvisionTask`'s `phase` is a free-form `str` needing no model change
   (GROUND.md section B), but reusing a collection named for provisioning for a
   sweep that is not provisioning is a naming lie.
5. **That #844's sentinel cleanup is per-probe and not per-provision**, so a
   sweep over N nodes does not leave N sentinels in `BIOINFO_HOME`.

## Out of scope

- **The probe itself.** #844. This child adds no verification of its own and
  must not grow one.
- **Enforcement.** #845, which merges after (Q5).
- **Periodic re-verification.** Decided yes at 6 hours (Q3), implemented in its
  own child — **file that issue when this merges.**
- **Setting up a share on a node found not-shared.** #847/#848. The report names
  the remedy; performing it is those children's.
- **Self-enrolled nodes gaining an SSH path.** A node with no `ssh_key_enc`
  stays unprobeable by this mechanism; giving it one is re-provisioning.
- **Inferring `Node.storage_location` for a node that has none.** Q2a: the
  operator supplies it or the node is reported unprobeable. Falling back to
  `ProvisionRequest`'s `/data/scratch` default is specifically rejected.
