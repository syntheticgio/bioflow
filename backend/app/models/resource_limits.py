"""The user's resource budget for admission decisions.

**An admission budget, not an enforced ceiling.** These numbers govern what
BioFlow plans to start; they do not cap or kill a running process. A job that
overruns its prediction goes over the limit, and that is an accepted outcome
-- see docs/superpowers/specs/2026-08-07-resource-limits-admission-design.md
for why enforcement-by-OOM-kill was rejected as the default.

The wording matters wherever these are surfaced: "will not plan to exceed",
never "will never exceed".

Exactly one document, and not a TimestampedDocument: it carries no `owner`,
deliberately. There is one machine here, matching the reasoning that leaves
AI provider settings unscoped -- a profile header should not change how much
memory the host has.
"""

from datetime import datetime
from typing import ClassVar

from beanie import Document
from pydantic import Field

from app.models.base import utcnow


class ResourceLimits(Document):
    """The stored budget. `None` on any field means "use the machine's own".

    None is a real state rather than a null needing cleanup: a fresh install
    has no opinion, and the machine's actual budget is the right default. The
    UI's "No limit" option writes None rather than a sentinel number.
    """

    SINGLETON_ID: ClassVar[str] = "resource_limits"

    id: str = Field(default=SINGLETON_ID)

    # Admission budget for memory. The one that actually binds today: it
    # replaces physical RAM as the ceiling `worker._free_resources` computes
    # headroom against, and `claim.lua` already refuses any job whose declared
    # mem_mb exceeds that headroom.
    max_mem_mb: int | None = None

    # Cores the governor may admit against.
    max_cpu: float | None = None

    # A default thread count ceiling for pipeline parameters. Advisory: it
    # bounds what the launch dialog offers, and does not stop a directly-called
    # API from asking for more.
    max_threads: int | None = None

    updated_at: datetime = Field(default_factory=utcnow)

    @classmethod
    async def load(cls) -> "ResourceLimits":
        """The limits document, creating it on first read.

        Upsert-on-read rather than a migration, for the same reasons AiRouting
        does it: there is exactly one, its empty state is meaningful, and a
        missing one is indistinguishable from a fresh install.
        """
        found = await cls.get(cls.SINGLETON_ID)
        if found is not None:
            return found
        created = cls()
        await created.insert()
        return created

    class Settings:
        name = "resource_limits"
