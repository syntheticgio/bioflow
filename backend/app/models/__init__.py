"""Beanie document models.

ALL_MODELS is what init_beanie registers; adding a model here is what creates
its indexes.
"""

from app.models.blob import Blob, BlobState, BlobStorage
from app.models.job import (
    ACTIVE_STATES,
    TERMINAL_STATES,
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
    SourceInfo,
    SourceMode,
)
from app.models.project import Project, ProjectCounters
from app.models.schedule import Schedule
from app.models.timing import JobRunTiming
from app.models.upload_session import UploadSession, UploadState

ALL_MODELS = [Project, Blob, DataObject, Job, UploadSession, Schedule, JobRunTiming]

__all__ = [
    "ACTIVE_STATES",
    "ALL_MODELS",
    "TERMINAL_STATES",
    "Blob",
    "BlobState",
    "BlobStorage",
    "Compression",
    "DataObject",
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
    "ObjectError",
    "ObjectRole",
    "ObjectStatus",
    "Project",
    "ProjectCounters",
    "Schedule",
    "SourceInfo",
    "SourceMode",
    "UploadSession",
    "UploadState",
]
