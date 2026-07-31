# Warming and persisting the tool probe cache

Removes the 6-15s stall that the first `/api/v1/pipelines/tools` request pays,
and stops `uvicorn --reload` from re-paying it on every backend edit.

Addresses the first entry in `docs/TODO.md`, raised 2026-07-31.

## The problem

Tool probing is lazy and serial, and nothing warms it. `all_tools()` calls
fifteen `lru_cache`d probe functions in sequence, each shelling out to
`<tool> --version`. No cache is populated at startup -- `lifespan` in
`backend/app/main.py` connects Mongo and Redis and loads handlers but never
touches `tools` -- so the entire probe cost is paid inside whichever user
request reaches `/api/v1/pipelines/tools` first. That is the tool selector and
the `/help/software` page.

Measured on this machine, cold container:

| | |
|---|---|
| NanoPlot alone | **12.0s** |
| All other 14 tools combined | ~2.7s |
| Full serial probe | **14.7s** |
| Endpoint, warm host page cache | **6.1s** |

This is one slow tool, not fifteen. NanoPlot imports pandas, scipy and plotly
before printing one line. That is why parallelism is the wrong fix: running all
fifteen probes concurrently caps the total at the slowest single probe, buying
about 3s of the 15 while adding a thread pool.

A second cost compounds it. `lru_cache` lives in the process, and this repo runs
under `uvicorn --reload` as its only mode (see CLAUDE.md). Every backend edit
discards all eighteen caches, so during active development the probe cost is
paid repeatedly rather than once.

## What this does not do

Not fixed here: asking NanoPlot for its version more cheaply. Option 2 in the
TODO entry -- `shutil.which` plus a version parsed from elsewhere -- would
collapse 12s to nearly zero, but loses the "does this binary actually execute"
check that `_probe`'s returncode branch was written for, which is what catches
an x86-64 binary on arm64. That check is worth more than the seconds.

Not fixed here: parallel probing, for the reason above.

## Design

Two pieces, layered. The first fixes the user-visible stall; the second fixes
the edit-reload loop and sits behind the first rather than being tangled into
it.

### Piece 1: warm the cache in `lifespan`, in the background

After `load_handlers()` in `backend/app/main.py`, a fire-and-forget
`asyncio.create_task` populates the probe caches before a user asks for them.

Three constraints on that task, each of which is a way this could go wrong:

- **It runs in a thread.** `all_tools()` is sync and spawns fifteen
  subprocesses. Calling it inline on the event loop would block every request
  for 15s, turning a latency problem into an outage. It goes through
  `asyncio.to_thread`.
- **It never gates readiness.** `/readyz` must not wait on it. A container that
  reports unready while probing gives a worse experience than the stall being
  fixed, and a probe that fails should not keep the app from serving.
- **It is never awaited, and its exceptions are logged.** A task whose exception
  is never retrieved logs a warning at garbage-collection time and is otherwise
  invisible; the task body catches and logs instead.

The existing laziness stays exactly as it is, as the fallback for a request that
arrives before the warm finishes. The point is to stop *guaranteeing* that a
user pays the probe cost, not to make it impossible.

### Piece 2: persist probe results in Redis

Redis rather than a file: it is already connected in `lifespan` immediately
before where the warm task runs, it adds no new storage concept, and staleness
self-corrects through TTL. A file under `.biopipe/` would survive full container
restarts too, but those happen far less often than reloads, and it would put a
cache file on the data drive where it competes conceptually with the host-side
capacity reporter that a later TODO entry wants to add there.

**The async/sync boundary decides the shape.** The Redis client is
`redis.asyncio`, while `all_tools()` and all eighteen probe functions are sync
and are called from sync contexts. A read-through cache *underneath* the
`lru_cache`s would mean reaching an async client from sync code in a worker
thread, via `run_coroutine_threadsafe` against the main loop -- coupling every
probe call to a running event loop that the worker process does not necessarily
have in the same shape.

So persistence lives at the `lifespan` layer only, where the code is already
async. The warm task and the cache load are the same task:

1. Read the stored entries from Redis.
2. For each tool, compute the fingerprint of its resolved binary. On a match,
   seed that tool's `lru_cache` directly with the stored `Tool`.
3. Probe whatever did not match, in the thread, as Piece 1 already does.
4. Write the full set back.

The sync probe functions do not change at all -- not their signatures, not their
bodies. Nothing outside `lifespan` learns that Redis is involved.

**Seeding an `lru_cache` from outside.** `functools.lru_cache` has no public API
for inserting a value. The mechanism is to call the wrapped function with its
(empty) argument list under a patched probe, which is fragile, so instead each
probe function gains a module-level override dict that `_probe` consults before
shelling out -- checked by fingerprint, so a stale entry cannot be served for a
changed binary. This keeps the seeding explicit and testable rather than relying
on cache internals.

**The fingerprint** is the resolved binary's `path + mtime + size`. An upgraded
tool produces a different fingerprint and is re-probed. This matters more than
raw cache-hit rate: versions end up in methods sections, so a stale version
string is a correctness bug, not a performance one. A tool that `shutil.which`
cannot resolve has no fingerprint and is always probed.

Entries carry a TTL (24h) as a backstop against a fingerprint that somehow fails
to change when the binary does.

### Failure posture

Redis unavailable, slow, or holding corrupt JSON must degrade to "probe
normally" -- never to an error, and never to a wrong answer. This follows the
same rule the governor entry in `docs/TODO.md` states for the mount sentinel: an
unavailable signal aborts the check rather than being read as bad news.

Concretely, every Redis interaction in the warm path is wrapped so that any
exception logs and falls through to probing. Because the warm task is itself
fire-and-forget and off the request path, the worst case of a total Redis
failure is exactly today's behaviour.

### `reset_cache()`

`reset_cache()` clears the eighteen `lru_cache`s and the override dict. It stays
sync, since tests call it directly.

Redis invalidation is a *separate* async function rather than being folded into
it. Folding them together would require `reset_cache()` to become async or to
reach for an event loop, which every existing sync caller would have to absorb.
The two are called together where both matter (a runtime config change); tests
that only want a clean in-process cache call `reset_cache()` alone, which is
correct because the seeding is fingerprint-checked and the override dict has
just been cleared.

## Testing

The seam to control is the Redis layer, not the subprocess.

CLAUDE.md warns about a specific failure here: the image ships most tools as
installed, so a test asserting the cache "works" passes whether or not its patch
took effect. Assert the direction that fails when the seam breaks:

- A fingerprint mismatch forces a re-probe -- store an entry, change the
  fingerprint, assert the probe runs. This is the correctness-critical case,
  since it is what keeps a stale version out of a methods section.
- An unreachable Redis still yields correct `Tool` objects. Patch the client to
  raise, assert `all_tools()` is unaffected.
- Corrupt JSON in a stored entry is ignored rather than raised.
- The warm task does not block startup: assert `lifespan` completes while a
  deliberately slow probe is still running.
- A failing warm task logs rather than propagating.

## Touches

- `backend/app/pipelines/tools.py` -- fingerprinting, the override dict,
  `reset_cache()`.
- `backend/app/main.py` -- the warm task in `lifespan`.
- `backend/app/db/redis_client.py` -- read only, for `get_redis()`.
- `backend/tests/pipelines/` -- the cases above.

## Consequences

Container start does up to fifteen subprocess spawns in a background thread that
it did not do before. It does not block readiness, but it is real work happening
at a moment when the machine may also be starting workers and Mongo.

Worth doing before another tool with NanoPlot's import shape is registered:
`all_tools()` has no per-tool budget, so a second heavy tool doubles the stall
that Piece 1 is moving off the request path.
