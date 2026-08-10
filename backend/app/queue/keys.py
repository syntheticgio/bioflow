"""Redis key layout for the queue.

Only small scalars live in Redis. Job payloads stay in MongoDB and are read by
id -- a 10 KB payload in a hash that gets scanned on every claim would make
dispatch progressively slower as the queue grows.
"""

PREFIX = "bp"

# --- Queues (sorted sets) ---
READY = f"{PREFIX}:q:ready"  # member=job_id, score=priority
DELAYED = f"{PREFIX}:q:delayed"  # member=job_id, score=available_at_ms
RUNNING = f"{PREFIX}:q:running"  # member=job_id, score=lease_expiry_ms

# --- Per-job dispatch metadata (hash) ---
JOB = f"{PREFIX}:job"  # bp:job:{job_id}

# --- Control ---
CANCEL = f"{PREFIX}:cancel"  # set of job_ids with cancellation requested
WORKERS = f"{PREFIX}:workers"  # hash worker_id -> json
NODES = f"{PREFIX}:nodes"  # hash node_id -> json (aggregated across workers)
EVENTS = f"{PREFIX}:events"  # pub/sub channel *prefix*, never published to

# Events are partitioned per profile: `events_channel(owner)` below. Nothing
# publishes to or subscribes to the bare `EVENTS` string any more -- a leftover
# subscriber on it would receive nothing at all and read as a broken stream
# rather than as a missing filter, so the prefix is deliberately not a valid
# channel on its own.
SYSTEM_OWNER = "system"
# Per-node ready queues: bp:q:ready:{node_id}
# Per-node concurrency counters: bp:conc:{resource}:{node_id}
"""Owner sentinel for events that belong to the installation, not a profile.

Storage faults and queue-wide conditions describe the machine; blobs are global
by design (see the profiles design doc), so there is no one profile to
attribute them to. Every SSE client subscribes to this channel alongside its
own. It cannot collide with a real owner: those are either the literal "local"
or a 24-character hex ObjectId.
"""

# --- Concurrency counters ---
CONC = f"{PREFIX}:conc"  # bp:conc:{resource}

# --- Scheduler ---
SCHED_NEXT = f"{PREFIX}:sched:next"  # bp:sched:next:{name}

# --- Leader election (reaper, promotion sweep, scheduler ticks) ---
LEADER = f"{PREFIX}:leader"


def events_channel(owner: str) -> str:
    """The pub/sub channel carrying one owner's events.

    Per-owner channels rather than one channel with an `owner` field on the
    payload, because the two fail differently: a publisher that forgets to
    stamp an owner would leak to every profile, while a publisher that picks
    the wrong channel merely emits where nobody listens. Events are advisory --
    the UI refetches on receipt -- so a missed one costs a delay, and a leaked
    one costs another profile's filenames.
    """
    return f"{EVENTS}:{owner}"


def job_key(job_id: str) -> str:
    return f"{JOB}:{job_id}"


def conc_key(resource: str, node_id: str | None = None) -> str:
    """Concurrency counter for a resource, optionally scoped to a node."""
    if node_id:
        return f"{CONC}:{resource}:{node_id}"
    return f"{CONC}:{resource}"


def ready_key(node_id: str | None = None) -> str:
    """Ready queue for a node, or the global pool."""
    if node_id:
        return f"{READY}:{node_id}"
    return READY


def sched_next_key(name: str) -> str:
    return f"{SCHED_NEXT}:{name}"


def node_conc_keys(node_id: str) -> list[str]:
    """All concurrency counter keys for one node."""
    return [conc_key(r, node_id) for r in ("cpu", "mem_mb", "io_heavy")]
