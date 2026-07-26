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
EVENTS = f"{PREFIX}:events"  # pub/sub channel

# --- Concurrency counters ---
CONC = f"{PREFIX}:conc"  # bp:conc:{resource}

# --- Scheduler ---
SCHED_NEXT = f"{PREFIX}:sched:next"  # bp:sched:next:{name}

# --- Leader election (reaper, promotion sweep, scheduler ticks) ---
LEADER = f"{PREFIX}:leader"


def job_key(job_id: str) -> str:
    return f"{JOB}:{job_id}"


def conc_key(resource: str) -> str:
    return f"{CONC}:{resource}"


def sched_next_key(name: str) -> str:
    return f"{SCHED_NEXT}:{name}"
