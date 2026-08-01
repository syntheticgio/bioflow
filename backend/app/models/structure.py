"""Cached gene-to-structure resolutions.

Its own collection rather than a key on the object, for the same reason as
`OrganismBlurb`: the answer is a property of the *gene in an organism*, not of
any one VCF. A yeast callset asks about the same 857 genes across every page
of its variants table and every project that calls against the same reference.

Negative results are stored, and are the majority: 65% of resolved genes have
no experimental structure. Caching only the hits would leave the common case
re-querying UniProt on every render.

Nothing here is authoritative. It is a lookup that can be recomputed at any
time, so the collection can be dropped without losing anything -- which is
also how a stale entry gets fixed, since UniProt gains structures over time
and this has no expiry.
"""

from pymongo import ASCENDING, IndexModel

from app.models.base import TimestampedDocument


class StructureLookup(TimestampedDocument):
    """What one gene resolved to in one organism."""

    gene: str
    # Scopes the entry. The same symbol in two organisms is two different
    # proteins, which is the collision this lookup is most concerned with, so
    # it is part of the key rather than a stored detail.
    taxid: int
    # None records a resolution failure -- the symbol matched nothing, or
    # every candidate was too short to hold the variant's residue. Kept as a
    # real answer so the miss is not re-queried.
    accession: str | None = None
    pdb_ids: list[str] = []
    # The resolved protein's length. Stored for provenance: it is what the
    # candidate was chosen by, and an entry whose length looks wrong is the
    # first sign the wrong protein was picked.
    length: int | None = None

    class Settings:
        name = "structure_lookups"
        indexes = [
            IndexModel(
                [("gene", ASCENDING), ("taxid", ASCENDING)],
                unique=True,
                name="uniq_gene_taxid",
            ),
        ]
