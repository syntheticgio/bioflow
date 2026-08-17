"""Beanie document models.

ALL_MODELS is what init_beanie registers; adding a model here is what creates
its indexes.
"""

from app.models.ai import AiProvider, AiRouting, FailureReason, ProviderKind, TaskSlot
from app.models.annotation_edit import AnnotationEdit
from app.models.app_settings import AppSettings
from app.models.blob import Blob, BlobState, BlobStorage
from app.models.drift import DriftCategory, DriftEntry, DriftReport
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
from app.models.local_database import LocalDatabase, LocalDatabaseCategory
from app.models.node import Node
from app.models.node_provision import NodeProvisionTask
from app.models.node_update import NodeUpdateTask
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
from app.models.protein_record import ProteinRecord
from app.models.resource_limits import ResourceLimits
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
from app.models.share import Share, ShareState
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
    AnnotationEdit,
    AiProvider,
    AiRouting,
    AppSettings,
    Project,
    Blob,
    DataObject,
    DriftReport,
    Job,
    UploadSession,
    Schedule,
    JobRunTiming,
    PipelineRun,
    RunJob,
    OrganismBlurb,
    FailureExplanation,
    Profile,
    ProteinRecord,
    StructureLookup,
    Feedback,
    LocalDatabase,
    Node,
    NodeProvisionTask,
    NodeUpdateTask,
    Share,
    ResourceLimits,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowNodeRun,
]

__all__ = [
    "ACTIVE_STATES",
    "ALL_MODELS",
    "AnnotationEdit",
    "AiProvider",
    "AiRouting",
    "AppSettings",
    "AttemptProgress",
    "TERMINAL_STATES",
    "Blob",
    "BlobState",
    "BlobStorage",
    "Compression",
    "DataObject",
    "DriftCategory",
    "DriftEntry",
    "DriftReport",
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
    "LocalDatabase",
    "LocalDatabaseCategory",
    "Node",
    "NodeProvisionTask",
    "NodeUpdateTask",
    "OPTIONAL_ROLES",
    "ObjectError",
    "ObjectRole",
    "ObjectStatus",
    "OrganismBlurb",
    "PipelineRun",
    "Profile",
    "ProfileDisplay",
    "Project",
    "ProjectCounters",
    "ProteinRecord",
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
