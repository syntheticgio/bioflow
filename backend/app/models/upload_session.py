"""Upload sessions: state for chunked/resumable uploads (exercised in Phase 2)."""

from datetime import datetime
from enum import StrEnum

from beanie import PydanticObjectId
from pydantic import Field
from pymongo import ASCENDING, IndexModel

from app.models.base import TimestampedDocument


class UploadState(StrEnum):
    OPEN = "open"
    ASSEMBLING = "assembling"
    HASHING = "hashing"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"
    EXPIRED = "expired"


class UploadSession(TimestampedDocument):
    project_id: PydanticObjectId
    filename: str
    total_size: int
    chunk_size: int
    total_chunks: int

    # Optional client-computed digest. Its value is pre-flight deduplication:
    # if we already hold this content, the upload transfers zero bytes.
    client_sha256: str | None = None

    state: UploadState = UploadState.OPEN
    received_chunks: list[int] = Field(default_factory=list)
    received_bytes: int = 0
    # Per-chunk digests, keyed by stringified index (Mongo keys must be strings).
    chunk_digests: dict[str, str] = Field(default_factory=dict)

    staging_dir: str
    assembled_path: str | None = None
    resulting_object_id: PydanticObjectId | None = None
    resulting_sha256: str | None = None

    # TTL reaps the document. It does NOT remove the staging files, so a
    # separate periodic job scans staging/ for orphaned directories.
    expires_at: datetime | None = None

    class Settings:
        name = "upload_sessions"
        indexes = [
            IndexModel([("expires_at", ASCENDING)], name="ttl", expireAfterSeconds=0),
            IndexModel([("project_id", ASCENDING), ("state", ASCENDING)], name="by_project"),
            IndexModel([("client_sha256", ASCENDING)], name="by_client_hash", sparse=True),
        ]
