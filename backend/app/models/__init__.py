"""Beanie document models.

ALL_MODELS is what init_beanie registers; adding a model here is what creates
its indexes.
"""

from app.models.ai import AiProvider, AiRouting, FailureReason, ProviderKind, TaskSlot
from app.models.blob import Blob, BlobState, BlobStorage
from app.models.conversation import ConversationTurn, ProjectConversation
from app.models.failure_explanation import FailureExplanation, normalize_failure
from app.models.feedback import Feedback
from app.models.job import (
    ACTIVE_STATES,
    TERMINAL_STATES,
    AttemptProgress,
    IoClass,
    Job,
    JobClass,
    JobError,
    JobLease,
    JobProgress,
    JobResources,
    JobState,
    JobTiming,
)
from app.models.object import (
    Compression,
    DataObject,
    FormatConfidence,
    FormatInfo,
    FormatKind,
    ObjectError,
    ObjectRole,
    ObjectStatus,
    SequenceType,
    SharedFrom,
    SidecarRole,
    SourceInfo,
    SourceMode,
)
from app.models.organism import OrganismBlurb, normalize_organism
from app.models.profile import Profile, ProfileDisplay
from app.models.project import Project, ProjectCounters
from app.models.resource_limits import ResourceLimits
from app.models.share import Share, ShareState
from app.models.run import (
    OPTIONAL_ROLES,
    PipelineRun,
    RunInput,
    RunInputRole,
    RunJob,
    RunJobRole,
    RunKind,
    RunStatus,
)
from app.models.schedule import Schedule
from app.models.structure import StructureLookup
from app.models.timing import JobRunTiming
from app.models.upload_session import UploadSession, UploadState
from app.models.workflow import (
    WorkflowBinding,
    WorkflowDefinition,
    WorkflowNodeRun,
    WorkflowRun,
)

ALL_MODELS = [
    AiProvider,
    AiRouting,
    Project,
    Blob,
    DataObject,
    Job,
    UploadSession,
    Schedule,
    JobRunTiming,
    PipelineRun,
    RunJob,
    OrganismBlurb,
    FailureExplanation,
    Profile,
    StructureLookup,
    Feedback,
    Share,
    ProjectConversation,
    ResourceLimits,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowNodeRun,
]

__all__ = [
    "ACTIVE_STATES",
    "ALL_MODELS",
    "AiProvider",
    "AiRouting",
    "AttemptProgress",
    "TERMINAL_STATES",
    "Blob",
    "BlobState",
    "BlobStorage",
    "Compression",
    "ConversationTurn",
    "DataObject",
    "FailureExplanation",
    "FailureReason",
    "Feedback",
    "FormatConfidence",
    "FormatInfo",
    "FormatKind",
    "IoClass",
    "Job",
    "JobClass",
    "JobError",
    "JobLease",
    "JobProgress",
    "JobResources",
    "JobRunTiming",
    "JobState",
    "JobTiming",
    "OPTIONAL_ROLES",
    "ObjectError",
    "ObjectRole",
    "ObjectStatus",
    "OrganismBlurb",
    "PipelineRun",
    "Profile",
    "ProfileDisplay",
    "ProjectConversation",
    "Project",
    "ProjectCounters",
    "ProviderKind",
    "ResourceLimits",
    "RunInput",
    "RunInputRole",
    "RunJob",
    "RunJobRole",
    "RunKind",
    "RunStatus",
    "Schedule",
    "SequenceType",
    "Share",
    "SharedFrom",
    "ShareState",
    "SidecarRole",
    "SourceInfo",
    "SourceMode",
    "StructureLookup",
    "TaskSlot",
    "UploadSession",
    "UploadState",
    "WorkflowBinding",
    "WorkflowDefinition",
    "WorkflowNodeRun",
    "WorkflowRun",
    "normalize_failure",
    "normalize_organism",
]
