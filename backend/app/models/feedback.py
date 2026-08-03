"""User feedback, submitted from the Help > Feedback page.

Write-only from the product's perspective -- there is no email or ticketing
integration behind this, just a collection someone reads with a shell or the
read-only list on the same page. Not owner-scoped: feedback is about the
installation, not any one profile's library, so every submission is visible
to everyone who opens the page, the same way schedules are global rather than
per-profile.
"""

from pydantic import Field
from pymongo import DESCENDING, IndexModel

from app.models.base import TimestampedDocument

CONTACT_MAX_LENGTH = 200
SUBJECT_MAX_LENGTH = 200
COMMENT_MAX_LENGTH = 2000


class Feedback(TimestampedDocument):
    contact: str = Field(max_length=CONTACT_MAX_LENGTH)
    subject: str = Field(max_length=SUBJECT_MAX_LENGTH)
    comment: str = Field(max_length=COMMENT_MAX_LENGTH)

    class Settings:
        name = "feedback"
        indexes = [IndexModel([("created_at", DESCENDING)], name="created_at_desc")]
