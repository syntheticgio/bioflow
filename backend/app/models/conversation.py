"""A project's Q&A chat history.

One document per (owner, project_id) pair -- a project's conversation is
per-profile, not shared across profiles that both happen to see the project,
matching every other collection's owner-partitioning.

`ConversationTurn` here holds only the outer user/assistant exchange visible
in the transcript. The in-flight tool-calling loop that produces an answer
has its own four-role `ConversationTurn` in `app.services.ai.adapters` for
its scratch state (tool_call/tool_result turns); those are never persisted --
only the final question and final answer land here. See
docs/superpowers/specs/2026-08-05-project-qa-chat-design.md.

The agent drawer (issue #97) reuses this model and its `ProjectConversation`
collection. It extends `ConversationTurn` with an optional `tool_calls` list
that records each MCP tool call the agent made inside an assistant turn --
name, args, result, and success -- so a reopened drawer shows the full
transcript including what tools ran. The Q&A path leaves `tool_calls` as None;
it is purely an agent-drawer field.
"""

from datetime import datetime
from typing import Any, Literal

from beanie import PydanticObjectId
from pydantic import BaseModel, Field
from pymongo import ASCENDING, IndexModel

from app.models.base import TimestampedDocument


class ToolCallTurn(BaseModel):
    """One tool call entry persisted inside a `ConversationTurn`.

    Only used by the agent drawer (issue #97); the Q&A path never sets it.
    Records the bioflow_* tool name (already unwrapped from the mcp
    proxy by `AgentProcess`), its arguments, the result summary, and
    whether it succeeded.
    """

    id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    result: str | None = None
    ok: bool | None = None


class ConversationTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime
    # Agent-drawer only: the tool calls made during this assistant turn.
    # None when the turn has no tool calls (every Q&A turn, or a user turn).
    tool_calls: list[ToolCallTurn] | None = None


class ProjectConversation(TimestampedDocument):
    project_id: PydanticObjectId
    turns: list[ConversationTurn] = Field(default_factory=list)
    # A condensed paragraph replacing turns[:compacted_through] once the live
    # tail threatens the routed provider's context window. Turns before this
    # index are retained on disk -- an honest transcript, never destroyed --
    # but excluded from what gets sent to the model.
    compacted_summary: str | None = None
    compacted_through: int = 0

    class Settings:
        name = "project_conversations"
        indexes = [
            IndexModel(
                [("owner", ASCENDING), ("project_id", ASCENDING)],
                name="uniq_owner_project",
                unique=True,
            ),
        ]
