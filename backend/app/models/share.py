"""Shares: an offer of one profile's file to another profile.

Deliberately outside the owner partition, for the same reason `Profile` is:
a Share describes a relationship *between* two partitions and belongs to
neither, so the inherited `owner` field is meaningless here. Every query names
`from_owner` or `to_owner` explicitly -- reading `owner` on this collection is
always a bug.

A share moves no bytes. It is one document plus, on acceptance, a second
`DataObject` pointing at a blob that already exists; see
docs/superpowers/specs/2026-08-05-profile-sharing-design.md.
"""

from enum import StrEnum

from beanie import PydanticObjectId
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.models.base import TimestampedDocument


class ShareState(StrEnum):
    OFFERED = "offered"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    WITHDRAWN = "withdrawn"  # sender revoked, before acceptance


class Share(TimestampedDocument):
    from_owner: str
    to_owner: str
    source_object_id: PydanticObjectId

    # Denormalized from the source object at offer time. Three reasons, and
    # the third is the load-bearing one: the inbox renders without reading
    # into the sender's partition; the list endpoint needs no per-row join;
    # and the offer still describes itself after the sender deletes their
    # copy, which is what lets acceptance refuse with "the sender deleted
    # this file" instead of dereferencing a missing object.
    name: str
    size: int
    blob_sha256: str

    state: ShareState = ShareState.OFFERED
    accepted_object_id: PydanticObjectId | None = None
    message: str | None = None

    class Settings:
        name = "shares"
        indexes = [
            # The recipient's inbox and the sender's outbox.
            IndexModel([("to_owner", ASCENDING), ("state", ASCENDING)], name="inbox"),
            IndexModel([("from_owner", ASCENDING), ("created_at", DESCENDING)], name="outbox"),
            # One pending offer per (sender, recipient, object). Partial on
            # OFFERED so that declining and re-offering the same file works,
            # and so a long history of accepted shares never blocks a new one.
            # Enforced here rather than by a read-then-insert in the service:
            # two concurrent offers both read "no pending offer" before either
            # writes, and the recipient gets the same file twice in their
            # inbox.
            IndexModel(
                [
                    ("from_owner", ASCENDING),
                    ("to_owner", ASCENDING),
                    ("source_object_id", ASCENDING),
                ],
                name="uniq_pending_offer",
                unique=True,
                partialFilterExpression={"state": ShareState.OFFERED.value},
            ),
        ]
