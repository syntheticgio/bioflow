"""Miscellaneous app-wide settings that don't warrant their own document.

Exactly one document, and not a TimestampedDocument: it carries no `owner`,
matching the reasoning in `resource_limits.py` -- one machine, one set of
settings, not profile-scoped.
"""

from datetime import datetime
from typing import ClassVar

from beanie import Document
from pydantic import Field

from app.models.base import utcnow


class AppSettings(Document):
    """The stored settings. Upserted on first read; a fresh install gets the
    field defaults below rather than needing a migration."""

    SINGLETON_ID: ClassVar[str] = "app_settings"

    id: str = Field(default=SINGLETON_ID)

    # Off by default: the Feedback page is hidden from the Help menu and its
    # route until a user opts in here.
    feedback_enabled: bool = False

    updated_at: datetime = Field(default_factory=utcnow)

    @classmethod
    async def load(cls) -> "AppSettings":
        found = await cls.get(cls.SINGLETON_ID)
        if found is not None:
            return found
        created = cls()
        await created.insert()
        return created

    class Settings:
        name = "app_settings"
