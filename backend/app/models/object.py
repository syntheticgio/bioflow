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
    # A BAM this pipeline produced. Format alone cannot carry it either: a BAM
    # from an alignment run and a BAM someone uploaded are the same format and
    # differ only in whether their provenance is known.
    ALIGNMENT = "alignment"
    # A VCF/BCF this pipeline called. Same reasoning as ALIGNMENT: an uploaded
    # VCF and a called one are the same format, and only the called one can say
    # which BAM, which reference, and which caller produced it.
    VARIANTS = "variants"
    # An assembly's authoritative annotation. Format says "intervals" and
    # cannot distinguish NCBI's published GFF3 from a user's peak calls or
    # blacklist, which are the same format used for a different purpose.
    ANNOTATION = "annotation"
    # Amino acid sequences. The role that matters most: a protein FASTA and a
    # reference genome are both FormatKind.FASTA, and only this keeps one out
    # of the aligner's reference picker.
    PROTEIN = "protein"
    # CDS / transcript nucleotide sequences. The same hazard as PROTEIN and
    # slightly worse: `cds_from_genomic.fna` is nucleotide FASTA that would
    # pass any "does this look like a genome" sniff test.
    TRANSCRIPT = "transcript"
    # Per-gene read counts for one sample. Anonymous TSV on disk, which is the
    # criterion this enum exists for: nothing in the bytes distinguishes a
    # counts table from any other tab-separated file, and only this keeps it
    # out of pickers that want a genome or an annotation.
    COUNTS = "counts"
    # The output of a differential expression test -- per-gene fold changes and
    # adjusted p-values. Also anonymous TSV, and deliberately a separate role
    # from COUNTS rather than a flag on it: feeding a results table back into a
    # DE run as if it were counts is exactly the silent error the split
    # prevents.
    DE_RESULTS = "de_results"


class SidecarRole(StrEnum):
    """What kind of scaffolding a sidecar is.

    Not an `ObjectRole`: role answers "how is this file used", and a sidecar is
    not used by a person at all. Keeping them separate is what lets the
    explorer filter scaffolding out by asking a single question.
    """

    BWA_MEM2_INDEX = "bwa-mem2-index"
    MINIMAP2_INDEX = "minimap2-index"
    BOWTIE2_INDEX = "bowtie2-index"
    HISAT2_INDEX = "hisat2-index"
    # One role for all eight files of STAR's genome directory. They are stored
    # flat, named `<reference>.STARindex.<member>`, and reassembled into a
    # directory at materialize time -- see aligners.IndexLayout.
    STAR_INDEX = "star-index"
    FAI = "fai"
    BAI = "bai"
    # The tabix index beside a bgzipped VCF -- to a VCF what BAI is to a BAM.
    TBI = "tbi"


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

    # Field names the user has explicitly set *or cleared*. Without it a
    # cleared role is indistinguishable from one never set, and re-ingest
    # re-asserts the role the user just removed -- which is precisely the
    # "never fight a user's explicit choice" promise above, broken.
    #
    # A list rather than a per-field `role_set_by` because the same ambiguity
    # applies to any user-editable field; metadata keys can join it unchanged.
    user_touched: list[str] = Field(default_factory=list)

    # Provenance. A typed field rather than a metadata key because metadata is
    # user-owned and user-editable, and provenance that can be silently retyped
    # is not provenance. A list because paired trimming takes two inputs and
    # produces two outputs, each descending from both mates.
    derived_from: list[PydanticObjectId] = Field(default_factory=list)
    produced_by_job: PydanticObjectId | None = None

    # The file this one accompanies. Distinct from derived_from, and the
    # distinction is what keeps the explorer usable: a trimmed FASTQ is a
    # specimen you search, annotate and align, while a `.bwt` is biologically
    # inert and means nothing away from its reference. Conflating them would
    # bury the files a user works with under scaffolding and make "what came
    # from this sample" unanswerable.
    #
    # The test for a future artifact is whether it is a specimen or scaffolding.
    sidecar_of: PydanticObjectId | None = None
    sidecar_role: SidecarRole | None = None

    # The other half of a paired-end run. Symmetric: both mates point at each
    # other. Inferred from the R1/R2 filename convention at ingest and
    # overridable, since the convention is only a convention.
    mate_object_id: PydanticObjectId | None = None

    # Which half of the pair this file is: 1 or 2. Set and cleared together
    # with mate_object_id -- a read number without a mate describes a pair
    # that does not exist. When inferred, it comes from the same
    # `pairing.split_mate` call that establishes `mate_object_id`, so the two
    # can never disagree; a manual pairing sets both explicitly instead.
    # Nullable for single-end files, and for pairs predating this field.
    #
    # A plain int rather than an enum: the domain is closed by biology at
    # {1, 2}, and an enum whose members are ONE and TWO reads worse at every
    # use site than the integer does. The request schema does the validating.
    read_number: int | None = None

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
            # "Does this reference have an index yet?" -- asked on every
            # alignment launch and to render index status in the explorer.
            IndexModel([("sidecar_of", ASCENDING)], name="by_sidecar_of"),
            IndexModel(
                [("format.kind", ASCENDING), ("project_id", ASCENDING)],
                name="by_format",
            ),
            # Lets arbitrary user metadata keys be queried without knowing them
            # ahead of time. Cannot be compound, so filtered queries still
            # scan-then-filter -- acceptable at single-user scale.
            IndexModel([("metadata.$**", ASCENDING)], name="metadata_wildcard"),
        ]
