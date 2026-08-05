"""Blobs: the content-addressed storage ledger.

One document per unique SHA-256. This is deliberately a separate collection from
`objects`: refcounting must be atomic and independent of how many objects point
at a piece of content. Putting a refcount on `objects` makes deduplication
unrepresentable -- two objects sharing content would each believe they own it.
"""

from datetime import datetime
from enum import StrEnum

from pymongo import ASCENDING, IndexModel

from app.models.base import TimestampedDocument


class BlobState(StrEnum):
    PENDING = "pending"  # record exists, bytes not yet placed
    PRESENT = "present"
    MISSING = "missing"  # confirmed absent after two consecutive checks
    QUARANTINED = "quarantined"  # size/mtime drift, or a hash collision candidate


class BlobStorage(StrEnum):
    MANAGED = "managed"  # lives under objects/, we own its lifecycle
    EXTERNAL = "external"  # registered in place; we never move or unlink it


class Blob(TimestampedDocument):
    # The SHA-256 hex digest IS the primary key.
    id: str  # type: ignore[assignment]

    size: int
    state: BlobState = BlobState.PENDING
    storage: BlobStorage = BlobStorage.MANAGED

    # Hash of the *uncompressed* stream, when this blob's stored bytes are
    # compressed (see storage/compress.py). None for a blob whose id already
    # is the plaintext hash -- an uncompressed blob, or one predating
    # compression. Kept distinct from `id`, which stays the hash of the bytes
    # actually on disk so the CAS invariant (id == sha256 of the stored file)
    # never breaks: dedup for compressible formats looks up by this field
    # instead, so two ingests of the same plaintext converge on one blob
    # regardless of which compressor (or the stdlib fallback) wrote it.
    content_sha256: str | None = None

    rel_path: str | None = None  # "ab/abcdef..." when managed
    external_path: str | None = None  # absolute path when external

    ref_count: int = 0

    first_seen_at: datetime | None = None
    last_verified_at: datetime | None = None
    verify_mode: str = "stat"  # stat | full
    # Consecutive failed existence checks. Two are required before declaring a
    # file missing, because external drives unmount transiently.
    miss_count: int = 0
    last_miss_at: datetime | None = None

    # Drift detection for external blobs, which we do not control.
    observed_mtime: float | None = None
    observed_size: int | None = None

    class Settings:
        name = "blobs"
        indexes = [
            # Drives the verifier's oldest-checked-first rotation.
            IndexModel(
                [("state", ASCENDING), ("last_verified_at", ASCENDING)],
                name="verify_rotation",
            ),
            # GC candidates only. Partial keeps the index tiny.
            IndexModel(
                [("ref_count", ASCENDING), ("updated_at", ASCENDING)],
                name="gc_candidates",
                partialFilterExpression={"ref_count": {"$lte": 0}},
            ),
            # One registration per external file. Must be *partial*, not
            # sparse: sparse only skips documents where the field is absent,
            # and managed blobs explicitly store external_path=null -- so a
            # sparse index would let the first managed blob succeed and every
            # subsequent one collide on null.
            IndexModel(
                [("external_path", ASCENDING)],
                name="uniq_external_path",
                unique=True,
                partialFilterExpression={"external_path": {"$type": "string"}},
            ),
            # Dedup lookup for compressed blobs. Partial + non-unique: absent
            # for every blob that predates compression or was never
            # compressible, and two blobs can share a content_sha256 only in
            # the window between a race's two placements, which
            # find_present_blob's PRESENT-state check already tolerates.
            IndexModel(
                [("content_sha256", ASCENDING)],
                name="content_sha256_lookup",
                partialFilterExpression={"content_sha256": {"$type": "string"}},
            ),
        ]
