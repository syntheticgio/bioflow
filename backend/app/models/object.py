"""Objects: the human-facing file entry inside a project.

An object is a *name and metadata* pointing at a blob. Several objects may share
one blob (deduplication), and an object exists before its blob does (while an
upload is still in flight).
"""

from datetime import datetime
from enum import StrEnum

from beanie import PydanticObjectId
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.models.base import TimestampedDocument


class ObjectStatus(StrEnum):
    UPLOADING = "uploading"
    HASHING = "hashing"
    INGESTING = "ingesting"  # detecting format / parsing headers
    READY = "ready"
    ERROR = "error"
    MISSING = "missing"  # underlying blob went away


class FormatKind(StrEnum):
    FASTQ = "fastq"
    FASTA = "fasta"
    BAM = "bam"
    SAM = "sam"
    CRAM = "cram"
    VCF = "vcf"
    BCF = "bcf"
    BED = "bed"
    GFF = "gff"
    GTF = "gtf"
    TEXT = "text"
    UNKNOWN = "unknown"


class Compression(StrEnum):
    NONE = "none"
    GZIP = "gzip"
    # BGZF matters well beyond "it's gzip": it is block-compressed and therefore
    # seekable/indexable, which determines whether tools can random-access it.
    BGZF = "bgzf"
    ZSTD = "zstd"
    BZIP2 = "bzip2"


class FormatConfidence(StrEnum):
    MAGIC = "magic"  # identified from file contents
    EXTENSION = "extension"  # guessed from the filename only
    USER = "user"  # explicitly overridden
    NONE = "none"


class FormatInfo(BaseModel):
    kind: FormatKind = FormatKind.UNKNOWN
    compression: Compression = Compression.NONE
    confidence: FormatConfidence = FormatConfidence.NONE
    # Recorded separately so a disagreement can be surfaced rather than silently
    # resolved -- a .bam that isn't a BAM is worth telling the user about.
    extension_says: FormatKind | None = None
    magic_says: FormatKind | None = None
    detected_at: datetime | None = None


class SourceMode(StrEnum):
    UPLOAD = "upload"
    REGISTER_IN_PLACE = "register_in_place"


class SourceInfo(BaseModel):
    mode: SourceMode = SourceMode.UPLOAD
    original_path: str | None = None
    original_name: str | None = None
    client_mtime: datetime | None = None


class ObjectError(BaseModel):
    code: str
    message: str
    at: datetime


class DataObject(TimestampedDocument):
    project_id: PydanticObjectId
    name: str  # human-facing, mutable, not unique
    blob_sha256: str | None = None  # null until hashing completes
    size: int = 0
    status: ObjectStatus = ObjectStatus.UPLOADING
    error: ObjectError | None = None

    format: FormatInfo = Field(default_factory=FormatInfo)
    # Parser output (read counts, sample names, reference contigs...). Schema
    # varies by format and grows over time, so it stays an open dict.
    facts: dict = Field(default_factory=dict)
    # User-assigned metadata. Phase 5 adds per-format schemas on top.
    metadata: dict = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)

    source: SourceInfo = Field(default_factory=SourceInfo)

    class Settings:
        name = "objects"
        indexes = [
            IndexModel(
                [("project_id", ASCENDING), ("created_at", DESCENDING)],
                name="project_listing",
            ),
            IndexModel([("project_id", ASCENDING), ("name", ASCENDING)], name="project_name"),
            # Reverse lookup: which objects reference this blob (refcount audit).
            IndexModel([("blob_sha256", ASCENDING)], name="by_blob"),
            IndexModel([("owner", ASCENDING), ("status", ASCENDING)], name="by_status"),
            IndexModel(
                [("format.kind", ASCENDING), ("project_id", ASCENDING)],
                name="by_format",
            ),
            # Lets arbitrary user metadata keys be queried without knowing them
            # ahead of time. Cannot be compound, so filtered queries still
            # scan-then-filter -- acceptable at single-user scale.
            IndexModel([("metadata.$**", ASCENDING)], name="metadata_wildcard"),
        ]
