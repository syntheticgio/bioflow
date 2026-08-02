"""Shared model conventions."""

from datetime import UTC, datetime

from beanie import Document
from pydantic import Field

SCHEMA_VERSION = 1


def utcnow() -> datetime:
    return datetime.now(UTC)


class TimestampedDocument(Document):
    """Every collection carries owner + timestamps + schema_version.

    `owner` is the profile partition key. It was carried unused from the start
    so that adding accounts later would be a code change rather than a data
    migration, and that is exactly how it played out: profiles shipped without
    rewriting a single document, because the first profile adopts the default
    `"local"` literally rather than being given an id of its own.

    Two consequences of that default worth knowing before relying on it:

    - **`"local"` is a real profile's owner, not a neutral sentinel.** It
      belongs to whichever profile adopted the pre-profiles library. Code that
      wants "belongs to the installation, not to anyone" must say
      `keys.SYSTEM_OWNER`, not `"local"` -- see `queue/scheduler.py`.
    - **A test asserting `owner == "local"` proves nothing**, since every
      document inherits it whether or not an owner was ever threaded through.
      The owner tests use non-`"local"` values on purpose.

    Blobs are deliberately *not* partitioned: `blob_rel_path` builds its path
    from the SHA-256 alone, so two profiles holding the same reference genome
    store it once. Only the metadata collections carry a meaningful `owner`.
    """

    owner: str = "local"
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    schema_version: int = SCHEMA_VERSION

    def touch(self) -> None:
        self.updated_at = utcnow()
