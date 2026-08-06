"""The project Q&A tool-calling loop.

Called synchronously from the `answer_project_question` THREAD handler (no
event loop of its own), which is why tool execution -- `execute_search_objects`
and `execute_list_jobs` are both async coroutines touching Mongo -- is
bridged per call via `run_from_thread`, the same pattern
`summary_handlers._resolve_sync` uses for `router.resolve`.

The loop is capped at `MAX_TOOL_CALLS`, enforced here rather than left to the
model's discretion -- nothing in either wire format lets a caller say "you
get 3 calls." If the cap is reached without the model producing a final
answer, one more call is made with `tools=None`, forcing a prose response
from whatever has already been learned rather than leaving the question
answered with nothing.
"""

import importlib

from app.logging import get_logger
from app.models.ai import FailureReason
from app.services.ai import qa_tools
from app.services.ai.adapters import Completion, ConversationTurn, Failure, ToolCall

# See summary_handlers.py for why this goes through sys.modules via
# importlib rather than `from app.services.ai import complete_sync` --
# app/services/ai/__init__.py rebinds the package attribute of the same
# name, shadowing the submodule.
_complete_mod = importlib.import_module("app.services.ai.complete")

log = get_logger(__name__)

MAX_TOOL_CALLS = 3

QA_SYSTEM_PROMPT = (
    "You answer questions about one bioinformatics project using only the "
    "search_objects and list_jobs tools. Never answer from prior knowledge "
    "about specific files or jobs -- if you have not called a tool for this "
    "question, say you don't know rather than guessing."
)

QA_TOOLS = [qa_tools.SEARCH_OBJECTS_SPEC, qa_tools.LIST_JOBS_SPEC]

# Names only, resolved against the qa_tools module at call time rather than
# cached as function references -- a dict built once at import time would
# capture qa_tools.execute_search_objects as it existed then, which a test's
# monkeypatch.setattr(qa_tools, "execute_search_objects", ...) never reaches.
# The same trap CLAUDE.md documents for aligner_registry's frozen specs.
_DISPATCH_NAMES = {"search_objects", "list_jobs"}


def complete_sync(*args, **kwargs):
    """Thin wrapper so tests have a seam that is not the real adapter call."""
    return _complete_mod.complete_sync(*args, **kwargs)


def run_from_thread(coro):
    """Bridge one async tool-execution coroutine onto the connect-time loop.

    Imported lazily, matching summary_handlers._resolve_sync's own late
    import -- this module is imported at handler-registration time, well
    before connect_to_mongo() has necessarily run.
    """
    from app.db.client import run_from_thread as _bridge

    return _bridge(coro)


def answer(
    *,
    provider,
    question: str,
    project_id,
    owner: str,
    prior_turns: list[ConversationTurn] | None = None,
) -> Completion | Failure:
    history: list[ConversationTurn] = list(prior_turns or [])
    history.append(ConversationTurn(role="user", content=question))

    for _ in range(MAX_TOOL_CALLS):
        result = complete_sync(
            provider, system=QA_SYSTEM_PROMPT, user="", history=history, tools=QA_TOOLS
        )
        if isinstance(result, Failure):
            return result
        if isinstance(result, Completion):
            return result

        # ToolCall.
        if result.name not in _DISPATCH_NAMES:
            log.warning("qa_unknown_tool", name=result.name)
            return Failure(FailureReason.BAD_RESPONSE, f"unknown tool: {result.name}")

        executor = getattr(qa_tools, f"execute_{result.name}")
        tool_result = run_from_thread(
            executor(result.arguments, project_id=project_id, owner=owner)
        )
        history.append(ConversationTurn(role="tool_call", tool_call=result))
        history.append(
            ConversationTurn(role="tool_result", tool_call_id=result.id, content=_dumps(tool_result))
        )

    # Exhausted -- force a final answer with tools withdrawn.
    return complete_sync(provider, system=QA_SYSTEM_PROMPT, user="", history=history, tools=None)


def _dumps(value: dict) -> str:
    import json

    return json.dumps(value)
