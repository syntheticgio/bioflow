"""Pending annotation edits — the draft overlay on a source object.

Issue #297. One document per (object, line, field): at most one edit for the
same column of the same source line. The compound unique index enforces this.

Edits whose new value equals the source's original column value are deleted
rather than stored, so an idempotent "revert to original" removes the record.

Materialization reads every edit, rewrites the tagged columns in the source
file, and writes a derived annotation object. On success the applier deletes
all edits for the source object from this collection -- they have been
consumed into the derived object.
"""

from datetime import UTC, datetime

from beanie import Document, PydanticObjectId
from pydantic import Field
from pymongo import ASCENDING, IndexModel


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AnnotationEdit(Document):
    """One column edit on one source line of an annotation object."""

    object_id: PydanticObjectId
    line: int  # 1-based source line
    field: str  # "source" | "type" | "start" | "end" | "attributes"
    old_value: str | None = None
    new_value: str
    owner: str

    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    class Settings:
        name = "annotation_edits"
        indexes = [
            IndexModel(
                [("object_id", ASCENDING), ("line", ASCENDING), ("field", ASCENDING)],
                unique=True,
                name="uniq_object_line_field",
            ),
        ]