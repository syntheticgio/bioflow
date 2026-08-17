"""The stored result of the storage drift sweep.

Exactly one document, like `AppSettings`: the sweep reports current state, and
#412 asks for a list to look at rather than a trend. History would cost a
growing collection to answer a question nobody asked.
"""

from datetime import datetime
from enum import StrEnum
from typing import ClassVar

from beanie import Document
from pydantic import BaseModel, Field

from app.models.base import utcnow

# Entry lists are capped so a pathological drift state cannot produce an
# unbounded document. Counts stay exact above the cap -- the number is the
# actionable part, and 500 examples is already more than anyone reads.
MAX_ENTRIES_PER_CATEGORY = 500


class DriftCategory(StrEnum):
    # A file under objects/ with no Blob record at all.
    ORPHANED_FILE = "orphaned_file"
    # A file whose Blob record is still PENDING past GC_GRACE: an ingest that
    # started and never finished. Distinct from ORPHANED_FILE because the
    # cause and the fix differ.
    STALLED_INGEST = "stalled_ingest"
    # A Blob record verify_files has confirmed absent. Not re-derived here.
    MISSING_BLOB = "missing_blob"
    # An object claiming a report whose directory is gone.
    MISSING_REPORT_DIR = "missing_report_dir"


class DriftEntry(BaseModel):
    category: DriftCategory
    path: str
    object_id: str | None = None
    digest: str | None = None
    size_bytes: int = 0


class DriftReport(Document):
    """The latest sweep. Upserted on first read, like `AppSettings.load`."""

    SINGLETON_ID: ClassVar[str] = "drift_report"

    id: str = Field(default=SINGLETON_ID)

    swept_at: datetime = Field(default_factory=utcnow)
    # True when the storage home was not mounted, in which case every blob
    # would look missing and the sweep refuses to draw conclusions.
    skipped: bool = False
    skip_reason: str | None = None

    counts: dict[str, int] = Field(default_factory=dict)
    entries: list[DriftEntry] = Field(default_factory=list)
    reclaimable_bytes: int = 0

    @classmethod
    async def load(cls) -> "DriftReport":
        found = await cls.get(cls.SINGLETON_ID)
        if found is not None:
            return found
        created = cls()
        await created.insert()
        return created

    class Settings:
        name = "drift_report"
