"""What this computation ran on.

Stamped on every record rather than looked up later, because the point is a
corpus that stays interpretable when it leaves this machine: a duration means
nothing without the hardware that produced it.

Both the raw totals and the cgroup budgets are recorded, because inside Docker
they differ and the budget is the one that binds -- psutil reports the Linux
VM's resources, not the host Mac's. A record claiming 32 GB when the container
was capped at 8 would skew the memory model here and skew an aggregated corpus
worse.
"""

import hashlib
import platform
import uuid

import psutil

_cached: dict | None = None


def capture() -> dict:
    """The host fingerprint. Probed once per process, then reused."""
    global _cached
    if _cached is None:
        _cached = _probe()
    return _cached


def reset_cache() -> None:
    """Drop the cached probe. For tests."""
    global _cached
    _cached = None


def _machine_id() -> str:
    """A stable, non-identifying hash of this host.

    `uuid.getnode()` is the MAC address, which is stable but identifying, so
    it is hashed and truncated. The result segments an aggregated corpus by
    hardware without naming anyone -- which is the whole reason this is not
    just the hostname.
    """
    raw = f"{uuid.getnode()}:{platform.machine()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _cpu_model() -> str:
    """A human-readable CPU name, or the architecture when unavailable.

    `platform.processor()` is empty on many Linux builds, which is why there
    is a fallback rather than a bare call.
    """
    return platform.processor() or platform.machine() or "unknown"


def _probe() -> dict:
    # Imported qualified, not `from ... import _read_cgroup_cpu`: tests patch
    # `app.queue.governor._read_cgroup_cpu` via monkeypatch, which only takes
    # effect on a lookup through the module object. A bare-name import bound
    # at call time would still work today, but hoisting it to module scope
    # (an easy "cleanup") would silently stop seeing the patch.
    import app.queue.governor as governor

    return {
        "cpu_model": _cpu_model(),
        "physical_cores": psutil.cpu_count(logical=False),
        "logical_cores": psutil.cpu_count() or 1,
        "total_ram_bytes": psutil.virtual_memory().total,
        "cgroup_cpu_budget": governor._read_cgroup_cpu(),
        "cgroup_mem_limit": governor._read_cgroup_mem(),
        "platform": f"{platform.system()}-{platform.release()}",
        "machine_id": _machine_id(),
    }
