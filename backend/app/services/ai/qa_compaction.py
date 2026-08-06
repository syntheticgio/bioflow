"""Threshold-triggered compaction of a project's Q&A history.

Triggered before building a new question's history, not on a schedule. Never
a truncation -- dropping the tail of what a user asked ten minutes ago
silently changes what "it" refers to in their next message. Instead, turns
before the newest exchange are folded into `compacted_summary` by one extra
model call, and `compacted_through` advances so the next question's history
is `[compacted_summary if present] + turns[compacted_through:]`.

The token estimate (`len(text) // 4`) is a coarse heuristic, not a real
tokenizer -- this codebase has no tokenizer dependency, and model-specific
tokenizers vary per provider/model in ways not worth chasing for a threshold
whose only job is "don't wait until the request 400s."
"""

import importlib

from app.config import settings
from app.models.conversation import ConversationTurn, ProjectConversation
from app.services.ai.adapters import Completion

# See qa.py / summary_handlers.py for why this goes through sys.modules via
# importlib rather than a direct `from app.services.ai import complete_sync`.
_complete_mod = importlib.import_module("app.services.ai.complete")


def complete_sync(*args, **kwargs):
    """Thin wrapper so tests have a seam that is not the real adapter call."""
    return _complete_mod.complete_sync(*args, **kwargs)


def _estimate_tokens(turns: list[ConversationTurn]) -> int:
    return sum(len(t.content) for t in turns) // 4


def needs_compaction(convo: ProjectConversation, *, context_length: int | None) -> bool:
    limit = context_length or settings.qa_default_context_tokens
    live_turns = convo.turns[convo.compacted_through :]
    return _estimate_tokens(live_turns) >= limit * settings.qa_compaction_threshold


def compact(convo: ProjectConversation, *, provider) -> None:
    """Mutates convo in place. Caller is responsible for saving it."""
    turns_to_fold = convo.turns[convo.compacted_through :]
    if not turns_to_fold:
        return

    transcript = "\n".join(f"{t.role}: {t.content}" for t in turns_to_fold)
    prior = f"Existing summary: {convo.compacted_summary}\n\n" if convo.compacted_summary else ""

    result = complete_sync(
        provider,
        system=(
            "Condense this conversation into a short paragraph of context, "
            "preserving anything the user would expect remembered."
        ),
        user=f"{prior}{transcript}",
    )
    if not isinstance(result, Completion):
        return

    convo.compacted_summary = result.text
    convo.compacted_through = len(convo.turns)
