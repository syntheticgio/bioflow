"""Cached prose about a species.

Its own collection rather than a key on the object, because the text is a
property of the *organism* and not of any one file: every E. coli FASTQ in every
project wants the same paragraph. Keying it by species means the model writes it
once and a project with forty runs of one organism reads it forty times for
free, instead of generating forty near-identical blurbs.

Nothing here is authoritative. It is page colour -- a couple of sentences of
background for someone looking at a file -- and it is regenerable at any time,
so the collection can be dropped without losing anything that matters.
"""

from datetime import datetime

from pydantic import Field
from pymongo import ASCENDING, IndexModel

from app.models.base import TimestampedDocument, utcnow


def normalize_organism(name: str) -> str:
    """The cache key: case- and whitespace-insensitive.

    'Homo sapiens', 'homo sapiens' and 'Homo  sapiens' are one species and must
    not become three cache entries. Deliberately no cleverness beyond that --
    strain suffixes genuinely distinguish organisms worth describing separately
    ('Escherichia coli K-12' is not 'Escherichia coli O157:H7'), so they stay
    part of the key.
    """
    return " ".join(name.split()).lower()


class OrganismBlurb(TimestampedDocument):
    """A few sentences of background about one species."""

    # The normalized key. Unique, so a race between two files of the same
    # organism cannot produce two rows.
    organism_key: str
    # What the user actually typed, for display and for the prompt -- the
    # lowercased key would read wrong in both.
    organism: str
    text: str
    model: str | None = None
    generated_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "organism_blurbs"
        indexes = [
            IndexModel([("organism_key", ASCENDING)], unique=True, name="uniq_organism"),
        ]
