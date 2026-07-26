"""Shared model conventions."""

from datetime import UTC, datetime

from beanie import Document
from pydantic import Field

SCHEMA_VERSION = 1


def utcnow() -> datetime:
    return datetime.now(UTC)


class TimestampedDocument(Document):
    """Every collection carries owner + timestamps + schema_version.

    `owner` is unused today (single-user, no auth) but present from the start so
    that adding accounts later is a code change, not a data migration.
    """

    owner: str = "local"
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    schema_version: int = SCHEMA_VERSION

    def touch(self) -> None:
        self.updated_at = utcnow()
