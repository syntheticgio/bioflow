"""Pipeline runs: the action a user asked for, and the jobs that served it.

Clicking Align once produces seven jobs -- an index build, the alignment, a BAM
index, and one header parse per produced file. The activity view showed that
decomposition rather than the work requested, and the information needed to
describe the request was stranded in one job's payload.

A run is a *user intent*, deliberately not an execution graph. It records what
was asked for and which jobs served it; it does not describe ordering, express
fan-out, or schedule anything -- `Job.depends_on` already does the scheduling
and is untouched by this. The test for anything added here is whether it
describes a user's request or the machine's plan; only the former belongs.
"""

from enum import StrEnum

from beanie import PydanticObjectId
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.models.base import TimestampedDocument


class RunKind(StrEnum):
    ALIGNMENT = "alignment"
    TRIM = "trim"
    SRA_DOWNLOAD = "sra_download"
    VARIANT_CALLING = "variant_calling"
    # Separate from SRA_DOWNLOAD because RunKind is a display and grouping
    # vocabulary, and "downloaded a genome" reads differently from "downloaded
    # sequencing runs" in the activity view.
    ASSEMBLY_DOWNLOAD = "assembly_download"
    # One member for both UniProt download shapes. A whole proteome and a
    # hand-picked set of proteins are the same request to the same endpoint --
    # only the query differs -- so splitting the enum would describe a
    # distinction the machine does not make. The run label carries it instead.
    UNIPROT_DOWNLOAD = "uniprot_download"
    # Counting reads per gene for one sample.
    QUANTIFY = "quantify"
    # The test across samples. Separate from QUANTIFY for the same reason
    # ASSEMBLY_DOWNLOAD is separate from SRA_DOWNLOAD -- this is a display and
    # grouping vocabulary, and "counted one sample" and "compared twelve of
    # them" are not the same line in an activity view. They are also genuinely
    # different shapes: every other member here describes a run with one or two
    # inputs, and this is the first with N.
    DIFFERENTIAL_EXPRESSION = "differential_expression"
    # De novo assembly. Distinct from ASSEMBLY_DOWNLOAD, which fetches a
    # published one -- the two produce the same kind of file by completely
    # different means, and a run list that conflated them would claim credit
    # for a genome NCBI assembled.
    ASSEMBLY = "assembly"
    # Reference-guided assembly work such as Pilon, RagTag, or iVar. Kept
    # separate from de novo assembly and assembly QC because it improves,
    # scaffolds, or derives an assembly from existing reference-like inputs.
    REFERENCE_ASSEMBLY = "reference_assembly"

    # Genome annotation: gene finding, functional annotation, and feature
    # coordinate extraction on a bacterial or archaeal assembly.
    ANNOTATION = "annotation"


class RunStatus(StrEnum):
    """Derived from member job states, never stored.

    A stored status would be a second source of truth about something the jobs
    already know, and it drifts the first time a write is lost -- the failure
    mode the queue's own reconciler exists for. This enum is the vocabulary of
    the API rather than a column.
    """

    WAITING = "waiting"  # nothing started yet
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    # Finished, but an optional member did not succeed. An alignment whose BAM
    # was produced and whose header parse failed is not a failed alignment: the
    # file is there and can be re-ingested.
    PARTIAL = "partial"


class RunInputRole(StrEnum):
    READS = "reads"
    MATE = "mate"
    REFERENCE = "reference"
    # A rough assembly that a reference-guided tool improves or scaffolds.
    DRAFT_ASSEMBLY = "draft_assembly"
    # The BAM a quantification counted.
    ALIGNMENT = "alignment"
    # Primer definitions for amplicon-aware reference assembly tools.
    PRIMERS = "primers"
    # The GTF/GFF it counted against.
    ANNOTATION = "annotation"
    # A per-sample count file going into a differential expression run. The
    # first role that appears many times in one run's `inputs`.
    COUNTS = "counts"
    # The assembly being annotated — distinct from DRAFT_ASSEMBLY, which is
    # for assemblies being improved or scaffolded.
    ASSEMBLY = "assembly"


class RunInput(BaseModel):
    object_id: PydanticObjectId
    # Copied rather than looked up. A run must stay readable after its inputs
    # are deleted; a record whose description dissolves with its inputs is not
    # a record. The id rides along so a still-present input can be linked.
    name: str
    role: RunInputRole


class PipelineRun(TimestampedDocument):
    kind: RunKind
    project_id: PydanticObjectId

    # "specimen_R1.fastq.gz -> ecoli_ref.fna". Built at launch, when every part
    # is known and present.
    label: str
    inputs: list[RunInput] = Field(default_factory=list)
    # Denormalized for the same reason as the input names: jobs are TTL-pruned
    # after 30 days, and a run described only by its jobs stops being
    # describable exactly when a record of what was run is most valuable.
    params: dict = Field(default_factory=dict)
    # Which tool actually ran this trim. None for non-trim runs (alignment
    # already names its tool via `params["aligner"]`, so this would be
    # redundant there rather than merely unset).
    tool: str | None = None
    outputs: list[PydanticObjectId] = Field(default_factory=list)

    class Settings:
        name = "pipeline_runs"
        indexes = [
            IndexModel(
                [("project_id", ASCENDING), ("created_at", DESCENDING)],
                name="project_listing",
            ),
            IndexModel([("created_at", DESCENDING)], name="recent"),
        ]


class RunJobRole(StrEnum):
    INDEX = "index"
    ALIGN = "align"
    TRIM = "trim"
    INDEX_BAM = "index_bam"
    INGEST = "ingest"
    DOWNLOAD = "download"
    QC = "qc"
    CALL_VARIANTS = "call_variants"
    QUANTIFY = "quantify"
    # The differential expression test itself. Not in OPTIONAL_ROLES below:
    # it is the whole point of its run, and a run reporting anything but
    # failure when it fails would be claiming a results table exists.
    TEST = "test"
    # Likewise not optional: an assembly run whose assembly failed produced
    # nothing.
    ASSEMBLE = "assemble"
    # Named for the action (consensus calling), not the pipeline family
    # (reference_assembly) -- matching how ALIGN and TRIM are named for
    # actions rather than QC's or ASSEMBLE's own PipelineType. The whole
    # point of its run, same reasoning as ASSEMBLE: a consensus run whose
    # consensus failed produced nothing.
    CONSENSUS = "consensus"
    # Short-read polishing of a draft assembly. Named for the action, same
    # reasoning as CONSENSUS above, and likewise the whole point of its run:
    # a polish run whose polish failed produced nothing.
    #
    # This and CONSENSUS were declared but never linked for a long while --
    # their launchers created no run at all. GitHub #91 gave
    # launch_consensus/launch_polish/launch_scaffold a
    # RunKind.REFERENCE_ASSEMBLY run each, so all three roles are now linked
    # at launch.
    POLISH = "polish"
    # Reference-guided scaffolding. Named for the action, same reasoning as
    # CONSENSUS and POLISH above.
    SCAFFOLD = "scaffold"
    # Genome annotation. The whole point of its run — an annotation run
    # whose annotation failed produced nothing.
    ANNOTATE = "annotate"


# Roles whose failure does not fail the run. The test is whether the expensive
# work survived: a failed header parse is recoverable by re-ingesting, and a
# failed QC by re-running QC, in both cases without repeating the download or
# the pipeline that produced the file.
#
# DOWNLOAD is deliberately *not* here. A download that fails produced nothing,
# so a run reporting anything but failure would be claiming a file exists.
OPTIONAL_ROLES: frozenset[RunJobRole] = frozenset(
    {RunJobRole.INGEST, RunJobRole.QC}
)


class RunJob(TimestampedDocument):
    """One job's membership in one run.

    A link collection rather than a list of job ids on the run, because a job
    can belong to more than one run: `build_index` is deduplicated by content,
    so a second alignment against the same reference reuses the first one's
    build. An array could not express that, which is the same reason a simple
    `run_id` field on Job was rejected.
    """

    run_id: PydanticObjectId
    job_id: PydanticObjectId
    role: RunJobRole
    # True when this run reused a job another run created. The run depended on
    # the work but did not cause it, and the UI shows it as reused rather than
    # omitting it or claiming credit.
    shared: bool = False

    class Settings:
        name = "run_jobs"
        indexes = [
            IndexModel([("run_id", ASCENDING)], name="by_run"),
            # "Which run does this job belong to?" -- how a job enqueued later
            # (index_bam, an ingest) finds the run it should join.
            IndexModel([("job_id", ASCENDING)], name="by_job"),
            # One membership per (run, job): re-linking on a retry must not
            # duplicate a member and double-count it in the derived status.
            IndexModel(
                [("run_id", ASCENDING), ("job_id", ASCENDING)],
                name="uniq_run_job",
                unique=True,
            ),
        ]
