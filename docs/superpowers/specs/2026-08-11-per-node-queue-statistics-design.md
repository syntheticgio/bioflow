# Per-node queue statistics and resource breakdown

Design for [#217](https://github.com/syntheticgio/bioflow/issues/217).
Written 2026-08-11.

## Problem

Per-node ready queues (`bp:q:ready:{node_id}`) and per-node concurrency
counters (`bp:conc:{resource}:{node_id}`) already exist in Redis, written by
`queue.enqueue()` and `claim.lua`. Nothing reads them in aggregate.

`GET /api/v1/system/load` reports whole-machine load and admission state with
no node awareness at all. `GET /api/v1/nodes` reports which workers are online
and what they are running, but not how many jobs are waiting for a given node.
So "is `gpu-node` backed up?" is not answerable from the UI, and a job queued
for a node ID that never enrolled -- a typo in `?target_node=` -- sits in a
queue nobody drains, invisible everywhere.

## Scope

Operational glance, not scheduling forensics. The question this answers is "is
this node backed up," refreshed on read. It deliberately does not answer "why
is this specific job not running."

`queued` therefore counts the **ready queue only**. Delayed jobs live in one
global sorted set (`bp:q:delayed`) with no node scoping, and blocked jobs are
not in Redis at all. Counting either per-node needs new bookkeeping on the
write path, which this design does not add.

## Components

### 1. `backend/app/queue/node_stats.py` (new)

Two public functions, both swallowing Redis errors rather than raising --
neither endpoint should return 500 because Redis hiccupped.

```python
async def node_stats(node_ids: Iterable[str]) -> dict[str, dict]
```

Returns `{node_id: {"queued": int, "cpu": int, "mem_mb": int,
"io_heavy": int}}`. One Redis pipeline for all nodes: a `ZCARD` on
`keys.ready_key(node_id)` and an `MGET` of `keys.node_conc_keys(node_id)` per
node. Missing keys read as zero; negative counters clamp to zero, matching the
existing `_node_conc` behaviour at `backend/app/api/v1/nodes.py`. On failure,
returns zeroes for every requested node.

```python
async def orphaned_queue_nodes(known: set[str]) -> list[str]
```

`SCAN`s `bp:q:ready:*`, strips the prefix, returns the IDs not in `known`.
`SCAN` rather than `KEYS` because `KEYS` blocks Redis; the keyspace is small
either way. On failure, returns `[]`.

The single-pipeline shape matters: `/nodes` currently issues one `MGET` per
online node in a loop, which this replaces.

### 2. `GET /api/v1/nodes`

- Each entry gains `queued_jobs: int`.
- The per-node `await _node_conc(node_id)` loop is replaced by one
  `node_stats()` call covering every node.
- `reserved` keeps its shape but is now populated for **offline** nodes too,
  rather than hardcoded to zeros. An offline node with nonzero reservations is
  a real condition -- workers died mid-job, counters not yet reaped -- and
  worth seeing.
- Orphaned queue nodes are unioned into the result, with `enrollment:
  "unknown"`, `online: false`, and zero workers. A stuck queue nobody drains
  is exactly what this table exists to surface.

`running_jobs` is **unchanged**: still summed from the worker heartbeat
`running` lists. That is a different number from a ready-queue-derived count,
and it is the right one here -- it reflects what workers report they are
actually executing.

### 3. `GET /api/v1/system/load`

`current_load()` keeps its existing behaviour -- return the governor's Redis
snapshot, or the psutil fallback when no leader has published -- and then
attaches a `nodes` key on **both** paths. Node stats are computed fresh at
request time rather than baked into the leader's published snapshot,
specifically so the feature does not vanish whenever the leader lock lapses:
that is the moment someone is most likely looking at the page.

Nodes are enumerated the same way `/nodes` does it (workers hash + Mongo
`Node` records), unioned with `orphaned_queue_nodes()`. Enumerating rather
than globbing is deliberate; the glob alone would miss enrolled nodes that
have no queued work.

Each entry:

```json
{
  "node_id": "gpu-node",
  "running": 2,
  "queued": 5,
  "cpu": 8,
  "mem_mb": 16384,
  "workers": 2,
  "known": true
}
```

`running` and `workers` come from the workers hash, the same derivation
`/nodes` uses. `known: false` marks a node ID that has a ready queue but no
enrollment record and no workers.

If enumeration fails, `nodes` is `[]` and a `nodes_error` string is attached,
following the `queue_error` precedent in `backend/app/api/v1/system.py` -- a
silent empty array reads as "no nodes" rather than "the read broke."

### 4. Frontend

`frontend/src/api/types.ts`:

- `NodeInfo` gains `queued_jobs: number`.
- New `SystemLoadNode` interface; `SystemLoad` gains `nodes: SystemLoadNode[]`
  and `nodes_error?: string`.

`frontend/src/components/SettingsNodes.tsx` gains a **Queued** column between
Running and Reserved CPU. The table stays on `/nodes` and does not poll
`/system/load`: `/nodes` answers "what machines do I have and are they
healthy," which is what this page is, and it carries the enrollment data
(hostname, `registered_at`, status, revoke) that `/system/load` has no
business knowing. Consistency between the two endpoints comes from the shared
helper, not from a shared fetch.

Orphaned nodes render with an "Unknown" status and a tooltip explaining that
jobs are queued for a node ID that has never enrolled.

## Testing

Backend, in `backend/tests/`, run from the worktree via
`./backend/run-worktree-tests.sh`:

- `node_stats()` against the suite's real Redis: correct counts, missing keys
  as zero, negative counters clamped.
- Orphan detection: push a job to a ready queue for an unenrolled node ID,
  assert it appears with `known: false` on `/system/load` and with
  `enrollment: "unknown"` on `/nodes`.
- Both endpoints: new keys present, shapes as specified.
- Redis-failure path: with the Redis call patched to raise, both endpoints
  still return 200 with zeroes, and `/system/load` carries `nodes_error`. This
  is the direction that fails when the error handling breaks -- a test
  asserting the happy path passes whether or not the seam works.

The frontend has no headless component-testing setup and none is expected.
Verification is the browser at `localhost:5273` via `./ops/worktree-up.sh`.

## Decisions and rejected alternatives

- **Per-node stats computed at request time, not published by the governor
  leader.** Baking them into the snapshot costs nothing per request but goes
  stale up to the snapshot TTL and is absent entirely on the
  `governor_active: false` fallback, which would make the `nodes` key
  intermittently missing.
- **A `nodes` key on `/system/load`, not a separate
  `/system/load/nodes` endpoint.** The issue asks for the former, and a
  separate endpoint means two polls from one page.
- **Orphaned queues included and flagged, not filtered out.** Costs one `SCAN`
  on a small keyspace and turns an invisible failure -- jobs queued forever
  for a misspelled node -- into a visible one.
- **The settings table stays on `/nodes`.** Switching it to `/system/load`
  would still require polling `/nodes` for enrollment data and merging
  client-side.
