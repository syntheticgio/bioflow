"""AI providers: adapters, routing, and the settings behind both.

Replaces the single-server `llm_client` module. The invariant that module
established still holds and is the reason this package exists in the shape it
does: **an AI call never raises into a job.** It returns a `Completion` or a
`Failure`, and the caller carries on either way.

What changed is that a failure now leaves a trace (see `complete.py`). The old
contract was written for one local server that is free to call and often simply
off, where silence costs nothing. Once keys and money are involved, an expired
key that silently stops producing summaries is a configuration problem the user
cannot see.
"""

from app.models.ai import FailureReason, ProviderKind, TaskSlot
from app.services.ai.adapters import Completion, Failure
from app.services.ai.complete import complete, complete_sync
from app.services.ai.router import ResolvedProvider, resolve

__all__ = [
    "Completion",
    "Failure",
    "FailureReason",
    "ProviderKind",
    "ResolvedProvider",
    "TaskSlot",
    "complete",
    "complete_sync",
    "resolve",
]
