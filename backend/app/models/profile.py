"""Profiles: the organizational boundary between people sharing one library.

Not a security mechanism. A profile's password, when set, exists only to stop
someone entering the *wrong* profile by accident -- see
docs/superpowers/specs/2026-07-31-profiles-design.md, "Passwords are a speed
bump". The rest of the API stays unauthenticated.

A Profile is deliberately outside the owner partition it defines: every other
collection is scoped by `owner`, and a Profile's own `owner` field (inherited
from TimestampedDocument, always "local") is meaningless -- what matters is the
value `owner_id()` returns, which becomes the `owner` value on every document it
creates. Read that method before assuming it is just the stringified `id`.
"""

from datetime import datetime

from pydantic import BaseModel, Field
from pymongo import ASCENDING, IndexModel

from app.models.base import TimestampedDocument


class ProfileDisplay(BaseModel):
    """Cosmetics for the profile picker. `username` is what actually matches.

    Emoji are safe here only because `owner` never becomes a path component:
    storage stays digest-addressed, so nothing in this class reaches a
    filesystem.
    """

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
    # Written on successful profile selection, not on every request that uses
    # the profile -- it orders the picker, it is not an activity log.
    last_used_at: datetime | None = None

    def owner_id(self) -> str:
        """The value this profile's documents carry in their `owner` field.

        Deliberately a method rather than `str(profile.id)` at each call site.
        First-boot adoption needs the very first profile created on an existing
        installation to own the documents already there, which carry the
        pre-feature default `owner: "local"` -- so for that one profile this
        accessor must return the literal `"local"` and no migration has to run.
        That branch lands with adoption; every caller going through here now is
        what makes it a one-line change then instead of a hunt through the
        services.

        The `"local"` will come from a stored flag, never from the id: `_id` is
        always a real ObjectId, because Beanie rejects a string id unless the
        model overrides the annotation the way `blob.py` does for its digest.
        """
        return str(self.id)

    class Settings:
        name = "profiles"
        indexes = [
            IndexModel([("username", ASCENDING)], name="uniq_username", unique=True),
        ]
