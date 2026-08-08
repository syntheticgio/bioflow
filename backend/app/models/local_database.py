"""A user-submitted database, tracked locally.

Write-and-list only, like `feedback.py` -- no edit or delete route exists
yet, since a mistaken entry is rare enough that removing it by hand in Mongo
is an acceptable cost for now. Not owner-scoped: this app is single-user, and
a per-profile split would just be a filter nobody needs.
"""

from enum import StrEnum

from pydantic import Field
from pymongo import DESCENDING, IndexModel

from app.models.base import TimestampedDocument

NAME_MAX_LENGTH = 200
URL_MAX_LENGTH = 2000


class LocalDatabaseCategory(StrEnum):
    """What kind of thing a submitted database is.

    Deliberately a small, purpose-built set -- not the 26-value free-text
    `c` field in data/databases.json (sized for a 1000+ entry reference
    catalog) and not sources.py's SOURCE_KINDS (a different classification
    for a different concept). The `label` is what the submit form and the
    list show; it lives here so adding a category is a one-place change.
    """

    REFERENCE_ASSEMBLY = "reference_assembly"
    ANNOTATION = "annotation"
    VARIANT_CLINICAL = "variant_clinical"
    TAXONOMY_METADATA = "taxonomy_metadata"
    PIPELINE_TOOL_DATA = "pipeline_tool_data"
    OTHER = "other"

    @property
    def label(self) -> str:
        return _CATEGORY_LABELS[self]


_CATEGORY_LABELS = {
    LocalDatabaseCategory.REFERENCE_ASSEMBLY: "Reference / Assembly",
    LocalDatabaseCategory.ANNOTATION: "Annotation",
    LocalDatabaseCategory.VARIANT_CLINICAL: "Variant / Clinical",
    LocalDatabaseCategory.TAXONOMY_METADATA: "Taxonomy / Metadata",
    LocalDatabaseCategory.PIPELINE_TOOL_DATA: "Pipeline / Tool Data",
    LocalDatabaseCategory.OTHER: "Other",
}


class LocalDatabase(TimestampedDocument):
    name: str = Field(max_length=NAME_MAX_LENGTH)
    url: str = Field(max_length=URL_MAX_LENGTH)
    category: LocalDatabaseCategory

    class Settings:
        name = "local_databases"
        indexes = [IndexModel([("created_at", DESCENDING)], name="created_at_desc")]
