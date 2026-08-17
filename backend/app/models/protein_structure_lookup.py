"""Cached accession-to-structure resolutions.

Deliberately *not* the `structure_lookups` collection, which serves the
variants table. That one is keyed by `(gene, taxid)` and carries a
sequence-length guard, because a gene symbol is not an identifier -- UniProt
may attach one symbol to several proteins, and the length is what tells them
apart.

An accession is an identifier. There is no ambiguity to guard against and no
residue position to guard with, so reusing that collection would mean stuffing
a non-gene key into a field called `gene` while defeating the guard that is the
whole reason the field exists.

Negative results are cached, and are the majority. Nothing here is
authoritative: it can be dropped and rebuilt, which is also how a stale entry
gets fixed, since UniProt gains structures over time and this has no expiry.
"""

from pymongo import ASCENDING, IndexModel

from app.models.base import TimestampedDocument


class ProteinStructureLookup(TimestampedDocument):
    """What one accession resolved to."""

    # The accession as queried -- a UniProt accession or an unversioned RefSeq
    # protein ID. Unique across both kinds, which is safe because their formats
    # cannot collide.
    accession: str
    # The UniProt accession that was selected. Differs from `accession` for a
    # RefSeq query, and is surfaced to the user so a mis-resolution is visible
    # rather than silent.
    resolved_accession: str | None = None
    protein_name: str | None = None
    pdb_ids: list[str] = []

    class Settings:
        name = "protein_structure_lookups"
        indexes = [
            IndexModel([("accession", ASCENDING)], unique=True, name="uniq_accession"),
        ]
