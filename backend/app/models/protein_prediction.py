"""Cached protein structure predictions, keyed by sequence hash.

A prediction is identified by the MD5 hash of the full amino acid sequence,
not by accession — a protein from an annotation tool has no accession but
still benefits from caching.
"""
from datetime import datetime

from beanie import PydanticObjectId
from pydantic import Field
from pymongo import ASCENDING, IndexModel

from app.models.base import TimestampedDocument


class ProteinPrediction(TimestampedDocument):
    """One sequence that has been predicted by a structure prediction model."""

    sequence_hash: str = Field(description="MD5 hex digest of the full sequence")
    sequence_length: int
    model_name: str
    model_version: str
    pdb_path: str = Field(description="Absolute path to the PDB file on disk")
    # pLDDT is stored on 0-1, not the 0-100 AlphaFold and ESMFold report it on.
    # `_parse_mean_plddt` in queue/prediction_handlers.py divides the PDB B-factor
    # column by 100 on the way in, and the frontend multiplies by 100 to display it.
    # Widening this bound to 100 would let an un-normalised value through silently.
    mean_plddt: float = Field(ge=0.0, le=1.0)
    plddt_per_residue: list[float] = Field(default_factory=list)
    source_object_id: PydanticObjectId
    source_ordinal: int
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Settings:
        name = "protein_predictions"
        indexes = [
            IndexModel([("sequence_hash", ASCENDING)], unique=True, name="uniq_sequence_hash"),
        ]
