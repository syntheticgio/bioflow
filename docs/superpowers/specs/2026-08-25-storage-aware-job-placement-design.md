# Storage-aware job placement — design

Date: 2026-08-25.

Closes [#845](https://github.com/syntheticgio/bioflow/issues/845). Child 2 of
[#843](https://github.com/syntheticgio/bioflow/issues/843).

**Depends on [#844](https://github.com/syntheticgio/bioflow/issues/844)** —
consumes `Node.storage_shared` (tri-state: `true` / `false` / `None` meaning
unknown), `Node.storage_location`, `Node.storage_checked_at`, and #844's
re-runnable probe endpoint. Nothing here establishes those; this enforces them.

## Why this child is not optional

`storage_shared` with nothing reading it is a flag that lies. The failure #843
exists to remove is not "the primary does not know" — it is a bucket of a
chunked alignment claimed by a node that cannot read the reference, failing
hours in with "Input reads not found", naming the file rather than the cause.
Recording the fact and then dispatching as though it were not recorded
reproduces that failure exactly, with a green field on the node row saying it
should not have happened.

The exclusion must be **selective**. A node with unshared storage still runs
`download_sra_run` and `download_assembly` correctly — those fetch their own
bytes from NCBI and write them where the applier is told to. Revoking such a
node wholesale would discard real capacity to fix a problem that touches a
subset of job types.

## What exists today

Verified against this worktree on 2026-08-25.

### Dispatch has two queues and no node-identity gate

- `queue/keys.py:78-82` — `ready_key(node_id)` returns `bp:q:ready:{node_id}`
  for a targeted job and the bare `bp:q:ready` for the global pool.
- `queue/queue.py:465-505` `_push_to_redis` writes the dispatch hash (`type`,
  `class`, `cpu`, `mem_mb`, `io`, `epoch`, `override`, `node`) and `ZADD`s the
  job into `keys.ready_key(target_node)`. `target_node` comes from the explicit
  parameter or, failing that, `target_node_ctx` (`api/deps.py:85`), an HTTP
  request-scoped ContextVar set by middleware.
- `queue/worker.py:298-311` `_try_claim` — **tries its own node queue first,
  then falls back to the global pool**:
  ```python
  claimed = await self._try_claim_queue(keys.ready_key(self.node_id), allowed, budgets)
  if claimed is None:
      claimed = await self._try_claim_queue(keys.ready_key(), allowed, budgets)
  ```
- `queue/scripts/claim.lua` gates on four things and only four: admitted
  **class**, **cpu**, **mem_mb**, **io_heavy**. It reads Redis hash fields; it
  has no access to Mongo and therefore none to `Node`.
- `chunked_align_handlers.py:56` enqueues per-bucket sub-jobs with **no**
  `target_node`, so they land in the global pool and any worker may claim one
  (GROUND.md section G).

### `Job` does not persist its target node

`_push_to_redis` writes `node` into the **Redis** hash only. `models/job.py`
has no `target_node` field. Consequence, already latent:
`queue.py:1023` `reconcile()` re-pushes every active job to the bare
`keys.READY`, and `queue.py:946`/`:987` (`rescue_orphans`, `_release_dependents`)
call `_push_to_redis(job)` with no `target_node`. **Node targeting does not
survive a Redis rebuild, a dependency release, or an orphan rescue.** Any
mechanism that expresses "only shared nodes may run this" by *which queue the
job is in* inherits that bug.

### `blocked_reason.py` covers resource gates, not placement

`queue/blocked_reason.py:22` — `GATES = ("class", "cpu", "mem", "io")`, the
fixed order mirrored in `claim.lua:118-133` and in the frontend's wording. The
reason is written by `claim.lua` for the head-of-queue candidate only
(`i == 1`), keyed `bp:why:{ready_key}` with a 15s TTL, and read advisorily —
`read()` swallows every error as "no reason available" (`:63-65`).
**It does not cover placement.** A job no node may claim would today produce
either no reason at all or, worse, a stale resource reason describing a gate
that is not the real one.

### `IoClass` is a throughput budget, not a storage-dependence signal

`models/job.py:58-63`:
```python
class IoClass(StrEnum):
    NONE = "none"
    LIGHT = "light"
    # More than a couple of concurrent heavy readers on a FUSE mount is slower
    # in aggregate than two, so this is a throughput cap as well as a safety one.
    HEAVY = "heavy"
```
Its sole consumer is the `io_heavy` concurrency counter in `claim.lua:117`.
See Decision Q2 for why this cannot be reused.

### The nodes table and its tri-state idiom

`frontend/src/components/SettingsNodes.tsx:144-152` — one flat table, no
per-node detail view. `NodeRow` at `:515-521` already carries an
Online/Offline/**Unknown** tri-state badge with an explanatory `title`.
`api/v1/nodes.py:98-106` is the mongo read loop that feeds it, carrying the
warning at `:107-114` that adding a field read here can silently empty
`mongo_nodes` for a stale fixture.

## Decision Q1: enforce at claim, in the worker, before the Lua script

**The worker asks "may I run this?" and answers it in Python, by passing a
narrowed set of admitted job types down into `claim.lua`.**

The three candidate placements and why two fail:

- **At enqueue.** Cannot work. The global pool exists precisely because the
  claiming node is not known at enqueue time — `chunked_align_handlers.py:56`
  enqueues N sub-jobs and any of N nodes may take any of them. Deciding
  placement at enqueue means pinning each sub-job to a node, which is #835's
  scheduling problem, not this one.
- **Inside `claim.lua`.** Cannot work as written. The script is Lua over Redis
  counters; `Node` lives in Mongo. Replicating `storage_shared` into Redis so
  Lua could read it means a second copy of a fact that changes on a probe, with
  no invalidation path — the class of bug where a node re-probes to `false` and
  keeps claiming because a cached hash field still says `true`.
- **A separate ready queue that only shared nodes poll.** Rejected. It expresses
  the policy as *queue membership*, and queue membership is exactly what does
  not survive `reconcile()` (`queue.py:1023`), `_release_dependents`
  (`queue.py:461`) or `rescue_orphans` (`queue.py:946`) — all three re-push to
  the bare `keys.READY`. The policy would hold until the first Redis restart
  and then silently stop holding, with no test that would notice.

So: the worker already computes what it may claim and passes it as an
argument. `_try_claim` (`worker.py:283-311`) reads the governor, builds
`allowed: set[str]` of admitted **classes**, computes budgets, and calls
`queue.claim(..., allowed_classes=sorted(allowed))`, which forwards it as
`ARGV[4]` into `claim.lua:51-54`. Storage exclusion is the same shape of
decision — a set the claiming worker knows and Lua does not — so it takes the
same route:

1. The worker resolves **its own** node's `storage_shared` and caches it.
2. When that is not `true`, it passes an additional `denied_types` argument.
3. `claim.lua` reads the already-written `type` field from the job hash
   (`_push_to_redis:479` writes it; `claim.lua:107` currently HMGETs five
   fields and does not include it) and skips a candidate whose type is denied.

This gives the property queue-membership cannot: the gate is evaluated **on
every claim attempt, from the live `Node` document**, so a re-probe takes
effect on the next claim rather than on the next enqueue. A job re-pushed to
the wrong queue by `reconcile()` is still refused by the node that must not run
it.

**Caching, and its bound.** The worker must not read Mongo on a path that runs
several times a second. It caches its own `storage_shared` with a TTL, on the
model of `_maintenance_starving` (`worker.py:334-340`), which caches a Mongo
query for 30s for exactly this reason. **60 seconds**, matching
`_OFFLINE_THRESHOLD_SECONDS` (`nodes.py:35`). The cost of the staleness window
is bounded and asymmetric, and must be stated: a node freshly probed to `false`
can claim filesystem-dependent work for up to 60 more seconds. A node freshly
probed to `true` waits up to 60 seconds before it starts claiming. The first is
the dangerous direction, so **the cache is invalidated eagerly on the probe
path** — #844's probe endpoint publishes an event the worker's existing
subscription already receives, and the worker drops its cached value on
receipt. The TTL is then the backstop for a missed event, not the mechanism.

**Fail closed.** If the worker cannot resolve its own `Node` document at all
(Mongo unreachable, node not enrolled), it treats itself as not-shared. That
matches Q3 and matches the `_revoked` posture already in `_claim_loop`
(`worker.py:256-260`), which stops claiming rather than guessing.

## Decision Q2: a new hand-maintained registry, keyed by job type. `IoClass` does not encode this.

**This is the decision most worth a second reader.** The tempting answer is
that `IoClass.NONE` versus `LIGHT`/`HEAVY` already tracks filesystem
dependence, and it would be a much cheaper answer if it were true. It is not,
and the counterexample is exact:

```python
# queue/sra_handlers.py:50-57
@handler(
    "download_sra_run",
    ...
    resources=JobResources(cpu=1, mem_mb=512, io=IoClass.HEAVY),
)
```

`download_sra_run` is `IoClass.HEAVY` and is **the** job the issue names as one
a non-shared node must keep running. Same for `download_assembly`
(`ncbi_assembly_handlers.py:51`), `fetch_remote` (`sra_handlers.py:155`) and
`download_kraken_db` (`kraken_handlers.py:96`) — all `HEAVY`, all self-fetching.

The reason is that the two properties are orthogonal. `IoClass` measures **how
much disk throughput a job consumes**, for a concurrency cap — its own
definition says so (`models/job.py:60-62`: "a throughput cap as well as a
safety one"), and its only consumer is the `io_heavy` counter. Filesystem
dependence is **whether the bytes the job needs already exist under the
primary's `BIOINFO_HOME`**. A download writes heavily and reads nothing; a
`summarize_object` reads a file and writes almost nothing. Deriving one from
the other would mis-classify in both directions.

Reusing `IoClass` is therefore rejected, and it must not be widened with a
fourth member either — that would make one field answer two unrelated questions
and quietly change the concurrency cap whenever the storage classification
changed.

### Which of CLAUDE.md's three kinds this is

Per CLAUDE.md's "Hand-maintained registries keyed by an enum": **genuinely
derivable — cover it exhaustively, with a companion `frozenset` for deliberate
omissions and a partition test.** This is the first kind, and it is the
pattern to copy.

It qualifies because the question has an answer for every registered handler.
There is no handler for which "does this read the primary's `BIOINFO_HOME`?" is
legitimately unanswerable — the handler's own body settles it. There is no open
vocabulary and no key owned outside this repo; `registry._HANDLERS`
(`queue/registry.py:158`) is populated by the `@handler` decorator in this
codebase and nothing else. 77 job types are registered today.

### Shape: a default plus a small exempt set, not two lists

Nearly every handler reads the primary's storage. Maintaining a 70-entry
"filesystem-dependent" list against a 7-entry exempt list would mean the
common case is the one someone forgets, and the forgetting direction would be
*unsafe*: a new handler omitted from the dependent list would be silently
allowed everywhere.

So the registry is inverted, and the default is the safe one:

```python
# backend/app/queue/storage_dependence.py

# Handlers that need nothing from the primary's BIOINFO_HOME beyond somewhere
# to write. Everything not listed here is filesystem-dependent -- the
# forgetting direction must be the safe one, so a new handler is excluded from
# non-shared nodes until someone deliberately says otherwise.
#
# Inclusion rule: a handler belongs here only if BOTH hold.
#   1. It obtains every input byte from outside the deployment (a network
#      fetch) or from its payload, never by reading an existing path under
#      BIOINFO_HOME.
#   2. Its output is applied by an applier running on the PRIMARY, or is
#      confined to node-local state, so nothing it wrote has to be readable
#      from the primary afterwards.
# `io=IoClass.HEAVY` is not evidence either way -- see the design's Q2.
SELF_CONTAINED_JOB_TYPES: frozenset[str] = frozenset({...})

def is_filesystem_dependent(job_type: str) -> bool:
    return job_type not in SELF_CONTAINED_JOB_TYPES
```

This is the `_NO_NARRATIVE_STEP` shape (`services/provenance_walker.py:180`),
which is a registry keyed by the same thing — a registered handler name — with
the same silent-skip failure mode, and its test
(`tests/services/test_provenance_verbs.py`) is the template for the one below.

### The membership argument, per candidate

**In the exempt set:**

- `download_sra_run`, `fetch_remote` (`sra_handlers.py:50`, `:146`),
  `download_assembly` (`ncbi_assembly_handlers.py:43`),
  `download_uniprot` (`uniprot_handlers.py:40`) — fetch from NCBI/EBI, stage
  under `tmp/`, and return a description for an applier that runs on the
  primary. These are the cases the issue names.
- `noop`, `sleep_test` (`handlers.py:94`, `:105`) — touch nothing.

**Deliberately *not* exempt, and this is the non-obvious half:**

- `download_kraken_db` (`kraken_handlers.py:90`) and `download_lineage`
  (`lineage_handlers.py:44`) also fetch from the network — but they extract
  into `settings.kraken_dbs` / `settings.lineages`, both derived from
  `bioinfo_home` (`config.py`, the 26 derived dirs). A node that downloads a
  9 GB Kraken database into its own private `/data` has produced a database the
  primary cannot see and that the next `classify_reads` on any other node
  cannot use, while the job reports success. That is a silent, expensive
  failure, and it is condition 2 of the inclusion rule doing its job.
- `verify_files`, `gc_blobs`, `sweep_storage_drift`, `verify_blob`
  (`handlers.py:475`, `:777`, `:1004`, `:73`) — maintenance that walks the
  blob store. Run on a node with a private `/data`, `verify_files` reports the
  primary's entire library missing and `gc_blobs` is a job whose whole purpose
  is deleting things. **The most dangerous entries in the table**, and exactly
  the ones an "it only downloads / it's just maintenance" intuition would wave
  through.
- `install_tool` / `uninstall_tool` (`tool_handlers.py:142`, `:193`) — verify
  against the installer before deciding; if installs land under a
  `bioinfo_home`-derived directory these are dependent, and if they are
  node-local image state they are exempt but useless to target. Listed in
  "Verify before implementing" rather than guessed at here.

### The tests, run together

Two tests, and per CLAUDE.md **the pair must be run together** — a fix that
adds an exemption can collide with a fix that removes one, and only the
partition test catches it:

```python
def test_every_registered_handler_is_classified():
    registry.load_handlers()
    names = set(registry.all_handlers())
    assert names, "registry empty -- handler modules were not imported"
    assert names <= (FILESYSTEM_DEPENDENT_JOB_TYPES | SELF_CONTAINED_JOB_TYPES)

def test_no_handler_is_both():
    assert not (FILESYSTEM_DEPENDENT_JOB_TYPES & SELF_CONTAINED_JOB_TYPES)
```

For the partition test to mean anything, the derivable half must be materialized
as a set rather than left implicit in the `not in`. `FILESYSTEM_DEPENDENT_JOB_TYPES`
is therefore computed at module scope from the live registry minus the exempt
set — which also makes the exhaustiveness assertion honest rather than vacuous,
and gives a third test its subject:

```python
def test_exempt_set_names_only_registered_handlers():
    """Reachability, in the other direction: an exemption for a handler that
    no longer exists is a typo that silently exempts nothing."""
```

The `assert names` guard against a vacuous pass is copied verbatim in intent
from `test_provenance_verbs.py:25-27`; without it an unimported registry makes
`set() <= anything` trivially true.

## Decision Q3: unknown is not-shared, and #846 ships first

`storage_shared is None` is treated identically to `False`. A node whose
storage has never been proven shared is a node that might have a private
`/data`, and the whole point of #844 was that the two are indistinguishable
without a round trip.

**The interaction with #846 must be stated plainly, because on its own this
decision breaks a working deployment.** Every node enrolled before #844 reads
`None`. The maintainer's own deployment has such nodes, running
filesystem-dependent work correctly today against hand-configured shared
storage. Merging #845 first stops that work — correctly, by this spec's own
rules, and disruptively, for no gain, since the answer was recoverable by
probing.

So: **#846 merges before #845.** #846 depends only on #844 and is a no-op for
a deployment with no pre-existing nodes; landing it first means that by the
time enforcement exists, the nodes that deserve `true` already have it. See
#846's spec, Decision Q5, which records the same ordering from the other side.
This is a hard ordering constraint, not a preference, and belongs in #845's PR
description.

## Decision Q4: a fifth gate, `storage`, in `blocked_reason`

Today the head-of-queue reason has four gates and none of them is placement.
The user-visible failure without this is the worst kind: a job sits at QUEUED
forever, the activity view infers a resource reason from a gate that is not
blocking it, and nothing anywhere says "no node can read your data."

Extend `GATES` to `("class", "cpu", "mem", "io", "storage")` — appended, not
inserted, because the order is "fixed, mirrored in claim.lua and in the
frontend's wording" (`blocked_reason.py:22`) and inserting would renumber a
contract three files share. `claim.lua`'s `i == 1` reason branch
(`claim.lua:118-133`) gains a corresponding arm, evaluated **last** so it never
masks a real resource reason.

`BlockedReason` gains no new fields; the `storage` gate carries none of
`need`/`free`/`admitted`, which are resource-shaped. The frontend maps the gate
to its own wording, as it already does for the other four.

**The user-facing string**, in the activity view:

> **Waiting for a node that can read your data.** This job reads files from
> the primary's storage, and no node currently online has been confirmed to
> share it. Check Settings → Nodes: a node showing "Storage: not shared" or
> "Storage: unknown" cannot run this job. Re-run the storage check from that
> node's row, or provision a node with shared storage.

It states what was found, why it is wrong, and the remedy — the #803 message
shape (GROUND.md section A) applied to a queue state instead of a provisioning
failure.

**The honest limit, and it must be documented rather than papered over.** The
reason is written by whichever worker last attempted a claim, is keyed by ready
queue (`blocked_reason.reason_key`, which deliberately ignores `node_id` —
`:36-42`), and expires in 15 seconds. On a deployment where *some* node can run
the job, that node's successful claim `DEL`s the key (`claim.lua:161`). So the
`storage` reason appears exactly when **every** polling worker refuses the head
job — which is the condition it is meant to describe. It will not appear if no
worker is polling at all; that is the offline case, which the nodes table
already shows. Do not try to fix this by writing the reason from the primary.

## Decision Q5: a Storage column on the nodes table, tri-state, with the exclusion named

`SettingsNodes.tsx`'s `NodeRow` already carries the idiom (`:515-521`): a badge
whose three states are distinguished by class, with the explanation in a
`title` rather than in the cell. Copy it exactly.

A new **Storage** column between Status and Version, three states from
`storage_shared`:

| Value | Badge | `title` |
|---|---|---|
| `true` | **Shared** | `Confirmed sharing the primary's storage at {storage_location} on {storage_checked_at}. This node can run every kind of job.` |
| `false` | **Not shared** | `This node's {storage_location} is not the primary's storage. It will only run jobs that fetch their own inputs (SRA and NCBI downloads). Set up a shared mount, then re-run the storage check.` |
| `None` | **Unknown** | `Storage has never been checked on this node. Until it is, it is treated as not shared and will only run jobs that fetch their own inputs. Run the storage check from this row.` |

The `false` and `None` titles say **what the node is excluded from**, in the
issue's words, and each names its remedy. The `unknown` wording deliberately
does not read as a soft state: it says the node is *treated as* not shared,
because that is the behaviour and a badge implying "we'll find out later" would
misdescribe it.

Each non-`true` row carries a control that invokes #844's re-runnable probe.
That endpoint is #844's; this spec only consumes it.

**Serving the three fields** means adding reads to the `enumerate_nodes` mongo
loop at `nodes.py:98-106` — which carries the explicit warning at `:107-114`
that a field read added there can silently empty `mongo_nodes` for a fixture
that lacks it, and that this has already happened once. Every test fixture and
mock constructing a `Node` must be checked in the same commit. Because
`storage_shared` is optional on the model, `doc.storage_shared` on a Beanie
document is safe; a `MagicMock` standing in for one is not.

## Requirements

- **R1.** A worker whose node's `storage_shared` is not `true` does not claim a
  job whose type is filesystem-dependent.
- **R2.** A worker whose node's `storage_shared` is not `true` does claim and
  complete a job whose type is in `SELF_CONTAINED_JOB_TYPES`.
- **R3.** Every job type registered in `registry._HANDLERS` is classified as
  either filesystem-dependent or self-contained.
- **R4.** No job type is classified as both.
- **R5.** A worker treats `storage_shared is None` identically to
  `storage_shared is False`.
- **R6.** A worker that cannot read its own `Node` document treats itself as
  not sharing storage.
- **R7.** A worker's cached storage status is discarded within 60 seconds of
  the `Node` document changing.
- **R8.** When the head-of-queue job is filesystem-dependent and every polling
  worker refuses it on storage, a user reading the activity view sees the
  `storage` blocked reason.
- **R9.** A user reading the nodes table sees each node's storage status as one
  of Shared, Not shared, or Unknown.
- **R10.** A user hovering a Not-shared or Unknown storage badge reads what
  that node is excluded from and one remedy.
- **R11.** A node whose `storage_shared` is `true` claims filesystem-dependent
  jobs exactly as it does today.

## Testing

- **R3/R4 together, always** — the exhaustiveness and partition tests are a
  pair per CLAUDE.md; run the whole class, never the one test a bug names. Plus
  the reachability test that the exempt set names only live handlers.
- **The `IoClass` counterexample, as a test** — assert that
  `download_sra_run` is both `IoClass.HEAVY` and self-contained. It is a
  one-line test that permanently documents why Q2 went the way it did, and it
  fails the moment someone tries to derive the classification from `io`.
- **R1/R2 at the claim boundary** — a fake worker with `storage_shared=False`
  against a ready queue holding one `align_reads` and one `download_sra_run`
  claims the download and leaves the alignment. Assert on *which job was
  claimed*, not on a helper's return value: the helper being right and the
  argument not reaching `claim.lua` is the failure mode a unit test of the
  helper would miss entirely.
- **R5** — the same test with `storage_shared=None`, asserting identical
  behaviour. Not a variant of the `False` test; a separate case, because the
  tri-state is where a truthiness bug (`if not node.storage_shared`) would pass
  the `False` test and still be wrong somewhere else.
- **R6** — patch the node lookup to raise; assert the worker claims the
  download and refuses the alignment. Fails closed, not open.
- **R7** — advance the cache clock past 60s and assert a second Mongo read; and
  assert the probe event drops the cache without waiting.
- **R11** — the regression that matters most. `storage_shared=True` claims
  `align_reads`. Without it, a bug that denies everything passes R1 and R2.
- **R8** — drive `claim.lua` directly against a real Redis with a denied type
  at the head, then read `blocked_reason.read()` and assert `gate == "storage"`.
  Also assert a job blocked on **memory** still reports `mem`, since the new
  arm is evaluated last and must not mask the others.
- **Frontend** — `SettingsNodes.tsx` has no jsdom setup, so the badge is a
  pure function extracted and tested under Vitest, per
  `AlignerParamFields.test.tsx`. Three inputs, three labels, three non-empty
  titles.
- **Real-data check, not a fixture** — per CLAUDE.md, check the rule against
  the real database: enumerate the live `Node` docs and confirm the classifier
  answers for every job type the deployment has actually run, using
  `timing_service.records_for_object` / the `job_timings` job-type list rather
  than the handler registry, to catch a type present in history but no longer
  registered.
- **`enumerate_nodes` fixtures** — after adding the three field reads, run the
  whole of `tests/api/test_node_provision.py` and every test touching
  `/nodes` or `/system/load`, watching for a node table that comes back empty
  rather than wrong (`nodes.py:107-114`).

## Verify before implementing

1. **#844's field names, as merged.** Its spec (Decision Q4) confirms
   `storage_shared: bool | None`, `storage_location: str | None`,
   `storage_checked_at: datetime | None`, with the invariant (its R16) that
   `storage_checked_at` is null **iff** `storage_shared` is `None`. Re-confirm
   against the merged code rather than the spec, then delete this item.
   Note #844's Decision Q3 allows an **opt-in enrolment of a node that probed
   `false`** (`allow_unshared_storage`). That node is enrolled, online, and
   correctly refused filesystem-dependent work by this spec — no special case
   is needed, but the nodes table's "Not shared" title must not read as though
   the node were misconfigured when the user chose it deliberately.
2. **Whether #844's probe publishes an event the worker already subscribes
   to.** Its `POST /nodes/{node_id}/check-storage` is synchronous and writes
   `storage_shared` directly (its Q5), which says nothing about an event. Q1's
   eager cache invalidation depends on one. If there is none, either add it
   here — a one-line `publish_event` on the probe's write path, which this
   child may own since it is the only consumer — or accept the 60s TTL as the
   sole mechanism and say so in the PR rather than leaving the invalidation
   aspirational.
3. **`install_tool` / `uninstall_tool`'s install root.** Read
   `tool_handlers.py` and whatever it calls: if installs land under a
   `bioinfo_home`-derived path they are filesystem-dependent; if they are
   node-local they are exempt. Do not guess — this is one of the two entries
   whose membership is not obvious from its name.
4. **`project_export`'s output path** (`pipeline_handlers.py:1111`) — an export
   written to a node-local `exports/` is a file the user can never download.
5. **Whether `claim.lua` HMGETing a sixth field costs anything measurable** at
   `CLAIM_SCAN_LIMIT = 50` (`queue.py:47`). Expected to be nothing; measure
   rather than assert it, since this is the hottest path in the queue.
6. **That the `override` precedent for appending an HMGET field holds** —
   `claim.lua:107-112` documents that `override` was "appended, so h[1]..h[5]
   keep their positions". Follow it exactly for `type`.

## Out of scope

- **Making `target_node` survive a Redis rebuild.** `reconcile()`,
  `_release_dependents` and `rescue_orphans` all lose it (see "What exists
  today"). It is a real pre-existing defect, this design deliberately routes
  around it rather than depending on it, and it warrants its own issue — file
  one, per CLAUDE.md's out-of-scope-issues rule.
- **Scheduling filesystem-dependent work *toward* shared nodes.** This spec only
  refuses; it never steers. Fan-out placement is #835.
- **Node-to-node data transfer** for the non-shared case. Explicitly out of
  scope for the whole epic (#843).
- **Setting up the share.** #847/#848.
- **Probing.** #844.
- **Migrating pre-existing nodes.** #846, which by Q3 ships first.
- **Revoking a non-shared node.** Rejected in the framing: selective exclusion
  is the point.
- **`chunked_align_handlers.py:57`'s job-id bug.** Tracked as #851.
