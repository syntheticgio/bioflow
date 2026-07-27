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


class ObjectRole(StrEnum):
    """How a file is *used*, when that cannot be read from its bytes.

    A reference genome and a set of reads can both be FASTA or FASTQ; which one
    a file is depends on the user's intent. Role records that intent, and is
    left unset for the common case where the detected format already implies
    the answer (a BAM is an alignment, a VCF is variants).

    Left as an enum rather than a boolean because formats such as WIG have more
    than two plausible roles; those extend this enum without a schema change.
    """

    REFERENCE = "reference"
    # Reads that a pipeline has already trimmed. Format alone cannot say this:
    # trimmed output is FASTQ exactly like its input, and feeding raw reads to
    # an aligner when you meant to feed trimmed ones is a silent error.
    TRIMMED_READS = "trimmed_reads"


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

    # None means "derive the category from the format". Only exceptions carry
    # a value, so re-ingest can never fight a user's explicit choice.
    role: ObjectRole | None = None

    # Provenance. A typed field rather than a metadata key because metadata is
    # user-owned and user-editable, and provenance that can be silently retyped
    # is not provenance. A list because paired trimming takes two inputs and
    # produces two outputs, each descending from both mates.
    derived_from: list[PydanticObjectId] = Field(default_factory=list)
    produced_by_job: PydanticObjectId | None = None

    # The other half of a paired-end run. Symmetric: both mates point at each
    # other. Inferred from the R1/R2 filename convention at ingest and
    # overridable, since the convention is only a convention.
    mate_object_id: PydanticObjectId | None = None

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
            IndexModel([("project_id", ASCENDING), ("role", ASCENDING)], name="by_role"),
            # "What did this file produce?" -- multikey over the array, so a
            # lookup by any one parent finds every descendant.
            IndexModel([("derived_from", ASCENDING)], name="by_derived_from"),
            IndexModel(
                [("format.kind", ASCENDING), ("project_id", ASCENDING)],
                name="by_format",
            ),
            # Lets arbitrary user metadata keys be queried without knowing them
            # ahead of time. Cannot be compound, so filtered queries still
            # scan-then-filter -- acceptable at single-user scale.
            IndexModel([("metadata.$**", ASCENDING)], name="metadata_wildcard"),
        ]
