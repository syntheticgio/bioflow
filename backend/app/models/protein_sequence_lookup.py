"""Cached sequence-to-structure resolutions.

The sibling of `ProteinStructureLookup`, keyed by sha256(sequence) instead of
accession. A sequence search is the fallback for records whose headers name
no identifier (issue #534): read the record's sequence by byte offset and query
UniProt for an exact match. Once the match yields a UniProt accession, the
result -- accession, protein name, and PDB IDs -- is the same shape as a
direct accession resolution.

A separate collection follows the same precedent R20 of the predecessor #477
design states for `ProteinStructureLookup` vs `StructureLookup`: don't mix
non-comparable key types in one collection. `ProteinStructureLookup` is keyed
by accession; this is keyed by sequence hash. They are different keys over
different lookups, and a single collection would force one index shape or
defeat the uniqueness guarantee of either.

Negative results are cached, and are the majority (most de novo predicted
proteins have no exact entry in UniProt). Nothing here is authoritative: it can
be dropped and rebuilt, which is also how a stale entry gets fixed, since a
no-match today may gain an entry tomorrow and this has no expiry.
"""

from pymongo import ASCENDING, IndexModel

from app.models.base import TimestampedDocument


class ProteinSequenceLookup(TimestampedDocument):
    """What one protein sequence resolved to, via UniProt sequence search."""

    # sha256 of the amino-acid sequence string (not including FASTA newlines).
    sequence_hash: str
    # The UniProt accession that was selected. None means "searched, not found"
    # -- a cached negative result, distinct from an outage which is never
    # cached so the caller can distinguish a final miss from a transient failure.
    resolved_accession: str | None = None
    protein_name: str | None = None
    pdb_ids: list[str] = []

    class Settings:
        name = "protein_sequence_lookups"
        indexes = [
            IndexModel(
                [("sequence_hash", ASCENDING)],
                unique=True,
                name="uniq_sequence_hash",
            ),
        ]
