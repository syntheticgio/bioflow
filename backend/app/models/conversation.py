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
"""

from datetime import datetime
from typing import Literal

from beanie import PydanticObjectId
from pydantic import BaseModel, Field
from pymongo import ASCENDING, IndexModel

from app.models.base import TimestampedDocument


class ConversationTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime


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
