"""Why the queue did not start the job at the head of the line.

`claim.lua` evaluates four independently-failing gates and, before #457,
returned nil without saying which one failed. It now records that decision;
this module is the read side.

The reason is advisory and short-lived (15s, matching the governor snapshot).
Every failure to read one is treated as "no reason available" rather than an
error: the activity view falls back to its own inference, which is what it
showed before this existed.
"""

import json
from dataclasses import dataclass

from app.logging import get_logger
from app.queue import keys

log = get_logger(__name__)

# Fixed order, mirrored in claim.lua and in the frontend's wording.
GATES = ("class", "cpu", "mem", "io")


@dataclass(frozen=True)
class BlockedReason:
    """One gate, and the numbers it compared."""

    gate: str
    need: int | None = None
    free: int | None = None
    job_class: str | None = None
    admitted: list[str] | None = None


def reason_key(ready_key: str = keys.READY) -> str:
    """Keyed by the ready queue, not the node id.

    `queue.claim` accepts a `node_id` but does not forward it to the script,
    so keying on it would always produce the global key while reading as
    though it were per-node.
    """
    return f"bp:why:{ready_key}"


async def read(redis, ready_key: str = keys.READY) -> BlockedReason | None:
    """The current reason, or None when there isn't a usable one."""
    try:
        raw = await redis.get(reason_key(ready_key))
        if not raw:
            return None
        data = json.loads(raw)
        gate = data.get("gate")
        if gate not in GATES:
            return None
        admitted = data.get("admitted")
        return BlockedReason(
            gate=gate,
            need=data.get("need"),
            free=data.get("free"),
            job_class=data.get("class"),
            admitted=admitted.split(",") if admitted else None,
        )
    except Exception as e:  # noqa: BLE001 - advisory data must never break a read path
        log.warning("blocked_reason_unreadable", error=str(e))
        return None
