"""Profiles: the organizational boundary between people sharing one library.

Not a security mechanism. A profile's password, when set, exists only to stop
someone entering the *wrong* profile by accident -- see
docs/superpowers/specs/2026-07-31-profiles-design.md, "Passwords are a speed
bump". The rest of the API stays unauthenticated.

A Profile is deliberately outside the owner partition it defines: every other
collection is scoped by `owner`, and a Profile's own `owner` field (inherited
from TimestampedDocument, always "local") is meaningless -- what matters is its
own `id`, stringified, which becomes the `owner` value on every document it
creates.
"""

from datetime import datetime

from pydantic import BaseModel, Field
from pymongo import ASCENDING, IndexModel

from app.models.base import TimestampedDocument


class ProfileDisplay(BaseModel):
    emoji: str = "🧬"
    colour: str = "#4a9eff"


class Profile(TimestampedDocument):
    username: str
    password_hash: str | None = None  # None means no password
    email: str | None = None
    display: ProfileDisplay = Field(default_factory=ProfileDisplay)
    # Free-form: name, institution, research areas. Never validated -- it is
    # display-only and has no effect on partitioning or behavior.
    details: dict = Field(default_factory=dict)
    last_used_at: datetime | None = None

    def owner_id(self) -> str:
        """The value this profile's documents carry in their `owner` field."""
        return str(self.id)

    class Settings:
        name = "profiles"
        indexes = [
            IndexModel([("username", ASCENDING)], name="uniq_username", unique=True),
        ]
