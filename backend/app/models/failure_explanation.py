"""Cached plain-language explanations of job errors.

Its own collection rather than a field on Job, because the same underlying
error recurs across many jobs and many users -- the same tool crash (e.g.
minimap2 exiting 1 on a missing index) produces the same code and message on
every occurrence, and keying the cache on that pair means it is explained
once and every later occurrence is a free indexed read, the same reasoning
OrganismBlurb (app/models/organism.py) uses for species background text.

Nothing here is authoritative. It is a plain-language restatement of a given
error string, regenerable at any time, so the collection can be dropped
without losing anything that matters.
"""

import hashlib
from datetime import datetime

from pydantic import Field
from pymongo import ASCENDING, IndexModel

from app.models.base import TimestampedDocument, utcnow


def normalize_failure(code: str, message: str) -> str:
    """The cache key: a hash of code and message together.

    Unlike normalize_organism's human-readable lowercase key, a hash is
    required here -- error messages are unbounded in length and character
    content (embedded paths, quotes, newlines), unsuitable as a literal
    indexed string. Code is hashed together with message, not separately:
    the same message text can mean different things depending on which code
    raised it ("no such file or directory" under a permanent config error
    reads differently than under a transient subprocess failure), so both
    must distinguish the key.
    """
    payload = f"{code}\x00{message}".encode()
    return hashlib.sha256(payload).hexdigest()[:32]


class FailureExplanation(TimestampedDocument):
    """A cached plain-language explanation of one job error."""

    # The cache key. Unique, so two jobs failing with the same error
    # concurrently cannot produce two rows.
    failure_key: str
    # Stored alongside the hash purely for inspectability -- a developer
    # reading this collection can see what a key maps to. Never queried on.
    code: str
    message: str
    text: str
    model: str | None = None
    generated_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "failure_explanations"
        indexes = [
            IndexModel([("failure_key", ASCENDING)], unique=True, name="uniq_failure"),
        ]
