# Multi-node compute: per-node queues, counters, and discovery

Date: 2026-08-10
Status: Design implemented, pending review

## Problem

BioFlow runs on one machine. The worker claims jobs from a single Redis queue
and executes them locally. This is correct for a single-user local tool, but a
second physical machine — even just a laptop next to a desktop — has no way to
participate in computation. The user can install BioFlow on computer 2 but it
sits idle, and computer 1 has no awareness of it.

## Goal

Computer 2 becomes a compute-only child node. It runs BioFlow's worker process
pointed at the *same* Redis and Mongo as computer 1. It discovers its own
resources, claims node-specific jobs, and reports results (files, timing data)
back to the shared database. The frontend on computer 1 shows connected nodes
and their resource usage.

This is Phase 1: single shared Redis/Mongo, no data transfer protocol, no SSH
setup. The child node must be on the same network and have direct Redis/Mongo
access. Fetching data to the child node (NCBI downloads, reference files on
`/data`) is left for Phase 2.

## Decisions

**A `WORKER_NODE_ID` config value identifies each physical machine.**
`settings.worker_node_id` defaults to `"primary"`. Every worker sets it
to a stable name at startup so the API can group worker processes by machine.

**Jobs are routed to a node via per-node Redis queues.** A job enqueued with
`target_node="gpu-node"` lands in `bp:q:ready:gpu-node` instead of the global
`bp:q:ready`. Workers on `gpu-node` claim from their node queue first, then
fall back to the global pool. A worker on a different node never sees the
node-specific jobs.

**Per-node concurrency counters (`bp:conc:*:{node}`) isolate resource
tracking.** A job claimed by a worker on `gpu-node` reserves resources against
`bp:conc:cpu:gpu-node`, not `bp:conc:cpu`. This means a saturated GPU node
does not block dispatching on the CPU node, and vice versa. Global
`bp:conc:cpu` (no suffix) remains for jobs enqueued without a target, which
are competed for by all nodes.

**Node identity is tracked end-to-end: enqueue → claim → release → reap.**
Every touchpoint records or reads the node:
- `_push_to_redis` writes `"node": target_node` into the Redis job hash
- `claim.lua` sets `node` on the hash and increments per-node counters
- `release.lua` reads `node` and decrements the matching per-node counters
- `reap_expired.lua` reads `node` and decrements the matching per-node counters

**`GET /api/v1/nodes` reports connected machines.** It reads the live
`bp:workers` hash, groups workers by `node_id`, and reports online status,
running job count, slot count, and per-node reserved resources. A worker
unseen past 60 seconds is offline.

**The reconcile path is unaffected.** `reconcile()` rebuilds the Redis
dispatch index from MongoDB. It does not know about node affinity, so
orphaned jobs land in the global `bp:q:ready`. This is correct: a lost
node's jobs should be picked up by any available worker rather than
stranded forever. The `node` field in the Mongo document is preserved
for auditing; the `node` field in the Redis hash is cleared.

## Not in scope (Phase 2)

- **Data transfer.** The child node needs a local scratch directory and a way
  to fetch inputs (NCBI downloads, reference files from computer 1's `/data`).
  Currently it must share the filesystem volume or download independently.
- **SSH setup / node enrollment.** No handshake protocol. The user configures
  Redis/Mongo URLs manually in the child node's `.env`.
- **Node selection in the UI.** The frontend doesn't yet expose a node picker
  when launching a pipeline. All pipeline jobs go to the global pool.
- **Resource reporting per-node.** The `queue_stats.snapshot()` endpoint
  reports aggregate queue statistics, not per-node breakdowns.

## Implementation checklist

- [x] `backend/app/queue/keys.py` — `ready_key(node_id?)`, `conc_key(resource, node_id?)`, `node_conc_keys(node_id)`
- [x] `backend/app/config.py` — `worker_node_id` setting
- [x] `backend/app/queue/worker.py` — `node_id` stored, passed to claim + register + reservation reads
- [x] `backend/app/queue/queue.py` — `enqueue()` accepts `target_node`, `claim()` accepts `node_id`/`ready_key`, `_push_to_redis` routes to per-node key
- [x] `backend/app/queue/scripts/claim.lua` — reads per-node conc keys, writes `node` to hash
- [x] `backend/app/queue/scripts/release.lua` — reads `node`, decrements per-node conc keys
- [x] `backend/app/queue/scripts/reap_expired.lua` — reads `node`, decrements per-node conc keys
- [x] `backend/app/api/v1/nodes.py` — `GET /api/v1/nodes`
- [x] Tests: `test_keys.py`, `test_nodes.py`, `test_per_node_claim.py`
- [ ] Frontend node status panel (future work)
- [ ] Node selection in pipeline launch UI (future work)

## How to add a child node

On computer 2:

1. Install BioFlow (clone the repo, `docker compose up -d` just for the image build)
2. Create `.env` pointing at computer 1's Redis and Mongo:

```env
MONGO_URL=mongodb://<computer-1-ip>:27017/biopipe?replicaSet=rs0&directConnection=true
REDIS_URL=redis://<computer-1-ip>:6379/0
WORKER_NODE_ID=child-laptop
BIOINFO_HOME=/data/scratch
```

3. Start only the worker (not api/web):

```bash
docker compose up -d worker
```

4. Verify on computer 1: `curl http://localhost:8000/api/v1/nodes` should show
   both `primary` and `child-laptop`.

The child node's worker will heartbeat into `bp:workers`, claim global-pool jobs,
and write results to the shared Mongo database. Frontend on computer 1 at
`localhost:5173` will reflect the child's work as it completes.

### Notes

- The child node needs network access to computer 1's Redis (6379) and Mongo (27017).
  No SSH tunnel, no VPN necessarily — just routable IPs.
- The child node's `/data` is separate. Pipeline jobs that read from disk
  (references, assembly files) will fail until Phase 2 adds data transfer.
  NCBI/SRA download jobs work immediately since they fetch from the internet.
- Computer 1's `docker-compose.yml` already exposes 27017 and 6379 to
  `0.0.0.0` via the `ports:` directives. If those are behind a firewall,
  the child node won't connect.
