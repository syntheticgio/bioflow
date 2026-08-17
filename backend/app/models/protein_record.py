"""One record of a protein FASTA, indexed at ingest.

Its own collection rather than a field on the object, because the population
is unbounded relative to the object: a human RefSeq protein set is roughly
120,000 records, and a fact document is not where 120,000 of anything belongs.

`facts["sequence_names"]` is deliberately not replaced by this. That list is
capped at 50 (`parsers.MAX_STORED_CONTIGS`), stores only the first whitespace
token, and feeds the Quality tab, which works. This collection answers a
different question -- "which proteins are in this file, and where is each one"
-- and the description and byte offset are the parts that make it able to.

The byte offset is stored for work this design defers rather than for anything
it does today: both follow-ups (sequence-similarity resolution, structure
prediction) need the bytes of one record, and an offset recorded during a pass
the ingest already makes is what keeps that cheap later.

Nothing here is authoritative. It is derived from a file that is itself the
source of truth, so the collection can be dropped and rebuilt by re-ingesting.
"""

from beanie import PydanticObjectId
from pymongo import ASCENDING, IndexModel

from app.metadata.protein_headers import RefKind
from app.models.base import TimestampedDocument


class ProteinRecord(TimestampedDocument):
    """One `>` record: what it is called, how long it is, and where it starts."""

    object_id: PydanticObjectId
    # Position in the file, 0-based. The list's stable sort key -- a name is
    # not unique within a FASTA and cannot order the list.
    ordinal: int
    # The header's first whitespace-delimited token, matching what
    # `parsers._parse_fasta` stores in `sequence_names`.
    identifier: str
    # Everything after that token. This is the part a person picks a protein
    # by, and the part the facts document drops.
    description: str = ""
    length: int
    byte_offset: int
    # What `protein_headers.parse_header` made of the header. None is the
    # ordinary outcome for annotation-tool output, not a parse failure.
    ref_kind: RefKind | None = None
    ref_accession: str | None = None

    class Settings:
        name = "protein_records"
        indexes = [
            # Orders the list and makes re-ingest collisions impossible.
            IndexModel(
                [("object_id", ASCENDING), ("ordinal", ASCENDING)],
                unique=True,
                name="uniq_object_ordinal",
            ),
            # Serves identifier search. Description search is a scoped
            # substring match rather than a text index: the corpus is at most
            # 150,000 short strings under a key already in hand, and a text
            # index would be a third index maintained at ingest for a query
            # that never spans objects.
            IndexModel(
                [("object_id", ASCENDING), ("identifier", ASCENDING)],
                name="object_identifier",
            ),
        ]
