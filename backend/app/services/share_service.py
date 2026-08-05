"""Offer, accept, decline, and revoke a share of one profile's file to another.

A share moves no bytes and enqueues no job: storage-level deduplication
already exists (`blobs` is global and refcounted), so a share is one `Share`
document plus, on acceptance, a second `DataObject` per shared file pointing at
the same blob. See docs/superpowers/specs/2026-08-05-profile-sharing-design.md.
"""

from datetime import UTC, datetime

from beanie import PydanticObjectId
from pymongo.errors import DuplicateKeyError

from app.api.deps import resolve_owner
from app.db.client import get_client
from app.errors import ConflictError, NotFoundError, ValidationError
from app.logging import get_logger
from app.models import (
    DataObject,
    ObjectStatus,
    Project,
    Share,
    SharedFrom,
    ShareState,
)
from app.queue import queue
from app.services import blob_service, object_service, project_service

log = get_logger(__name__)

SHARED_WITH_ME_PROJECT_NAME = "Shared with me"


async def offer_share(
    *,
    owner: str,
    object_id: PydanticObjectId,
    to_profile_id: str,
    message: str | None = None,
) -> Share:
    """Offer one of your objects to another profile.

    Resolves the target through the same `deps.resolve_owner` the header goes
    through, so "share with profile X" and "act as profile X" agree on what a
    valid profile id is, including the `"local"` special case for the adopted
    profile -- whose `owner_id()` is not its `str(id)`.
    """
    obj = await object_service.get_object(object_id, owner=owner)  # owner-scoped: 404 for others

    to_owner = await resolve_owner(to_profile_id)
    if to_owner == owner:
        raise ValidationError("Cannot share a file with yourself")

    if obj.status is not ObjectStatus.READY:
        raise ConflictError(
            f"Only a ready file can be shared; this one is {obj.status.value}.",
            details={"status": obj.status.value},
        )
    if not obj.blob_sha256:
        raise ConflictError("This file has no stored content yet")

    share = Share(
        from_owner=owner,
        to_owner=to_owner,
        source_object_id=obj.id,
        name=obj.name,
        size=obj.size,
        blob_sha256=obj.blob_sha256,
        message=message,
    )
    try:
        await share.insert()
    except DuplicateKeyError as e:
        raise ConflictError(
            f"{obj.name!r} is already offered to that profile and awaiting a response",
            details={"object_id": str(obj.id)},
        ) from e

    await queue.publish_event(
        "share.offered",
        {"share_id": str(share.id), "from_owner": owner, "name": obj.name},
        owner=to_owner,
    )
    return share


async def list_inbox(*, owner: str, state: ShareState | None = ShareState.OFFERED) -> list[Share]:
    """Shares offered *to* this profile. Never reads `Share.owner` -- that
    field is inherited from TimestampedDocument and meaningless here, since a
    Share straddles two partitions rather than belonging to one."""
    query: dict = {"to_owner": owner}
    if state is not None:
        query["state"] = state.value
    return await Share.find(query).sort("-created_at").to_list()


async def list_outbox(*, owner: str) -> list[Share]:
    """Shares this profile made, every state. The sender's view is a history,
    and an accepted share is the row they most want to see -- unlike the
    inbox, this is not filtered to OFFERED by default."""
    return await Share.find({"from_owner": owner}).sort("-created_at").to_list()


async def load_for_recipient(share_id: PydanticObjectId, *, owner: str) -> Share:
    share = await Share.get(share_id)
    if share is None or share.to_owner != owner:
        raise NotFoundError(f"Share not found: {share_id}")
    return share


async def _load_for_sender(share_id: PydanticObjectId, *, owner: str) -> Share:
    share = await Share.get(share_id)
    if share is None or share.from_owner != owner:
        raise NotFoundError(f"Share not found: {share_id}")
    return share


async def decline_share(*, owner: str, share_id: PydanticObjectId) -> Share:
    """Refuse a pending offer. Terminal -- the sender can offer the same file
    again, which creates a new Share rather than reviving this one, so the
    history stays readable."""
    share = await load_for_recipient(share_id, owner=owner)
    if share.state is not ShareState.OFFERED:
        raise ConflictError(
            f"This share is already {share.state.value}",
            details={"state": share.state.value},
        )
    share.state = ShareState.DECLINED
    await share.save()
    return share


async def revoke_share(*, owner: str, share_id: PydanticObjectId) -> Share:
    """Withdraw an offer the recipient has not accepted.

    An *accepted* share cannot be revoked, and the refusal is explicit rather
    than a silent no-op. Once materialized, the copy is an ordinary DataObject
    in the recipient's library: deleting it would let one profile destroy
    another's file -- and anything derived from it -- through an
    unauthenticated endpoint, and would break the one-owner-per-object
    invariant every owner-scoped service is built on.

    It would also buy nothing. The API is unauthenticated by design (see the
    profiles spec, "Passwords are a speed bump"): a recipient who wanted the
    file back could send the sender's profile id in a header. Withdrawing an
    unaccepted offer is a real action; deleting an accepted copy only looks
    like one. See the design note, "Offer, accept, decline, revoke".
    """
    share = await _load_for_sender(share_id, owner=owner)
    if share.state is ShareState.ACCEPTED:
        raise ConflictError(
            "This file was already accepted and now belongs to the other "
            "profile. Ask them to delete their copy.",
            details={"state": share.state.value},
        )
    if share.state is not ShareState.OFFERED:
        raise ConflictError(
            f"This share is already {share.state.value}",
            details={"state": share.state.value},
        )
    share.state = ShareState.WITHDRAWN
    await share.save()
    return share


async def _shared_with_me_project(owner: str) -> Project:
    """Lazily create the recipient's "Shared with me" project.

    An ordinary Project, not a special kind: it inherits listing, counters,
    cascade delete and pipeline launch with no second case anywhere. Created on
    first acceptance rather than at profile creation -- an empty "Shared with
    me" in every new library is clutter that explains nothing.

    `uniq_sibling_name` is (owner, parent_id, name), so two profiles can each
    have one. The ConflictError catch handles two acceptances racing to create
    it at once.
    """
    existing = await Project.find_one(
        {"owner": owner, "parent_id": None, "name": SHARED_WITH_ME_PROJECT_NAME}
    )
    if existing is not None:
        return existing
    try:
        return await project_service.create_project(name=SHARED_WITH_ME_PROJECT_NAME, owner=owner)
    except ConflictError:
        project = await Project.find_one(
            {"owner": owner, "parent_id": None, "name": SHARED_WITH_ME_PROJECT_NAME}
        )
        if project is None:  # pragma: no cover - the race that just lost still finds a winner
            raise
        return project


def _copy_for(
    src: DataObject,
    *,
    owner: str,
    project_id: PydanticObjectId,
    share: Share,
    now: datetime,
) -> DataObject:
    """One recipient-side copy of one source object.

    Carried over: name, size, format, facts, metadata, tags, role, source, and
    `user_touched`. That last one is not obvious and is deliberate -- it
    records which fields a *person* set, and dropping it would let the
    recipient's next re-ingest overwrite a role the sender chose and the
    recipient never cleared.

    Cleared: `derived_from` and `produced_by_job`, which name objects and jobs
    in the sender's partition; the recipient's owner-scoped lookups resolve
    neither, so carrying them forward would render provenance links that 404.
    `shared_from` replaces both.

    `sidecar_of` and `mate_object_id` are rewritten by the caller once the new
    ids exist -- copying them verbatim would point into the sender's
    partition, which `list_sidecars`/pairing lookups (owner-scoped) can never
    resolve.

    This enumerates `DataObject`'s fields by hand rather than deriving the copy
    from the model, so a field added to `DataObject` later defaults to being
    silently DROPPED from a shared copy unless this function is updated in
    lockstep -- the opposite of `derived_from`'s carry-by-default failure mode.
    Worth revisiting as a `model_copy(update=...)` with an explicit
    never-carried allowlist if `DataObject` keeps growing.
    """
    return DataObject(
        project_id=project_id,
        owner=owner,
        name=src.name,
        size=src.size,
        status=ObjectStatus.INGESTING,  # attach_existing_blob_to_object sets READY
        format=src.format,
        facts=dict(src.facts),
        metadata=dict(src.metadata),
        tags=list(src.tags),
        role=src.role,
        user_touched=list(src.user_touched),
        sidecar_role=src.sidecar_role,
        read_number=src.read_number,
        source=src.source,
        shared_from=SharedFrom(
            object_id=src.id, owner=src.owner, share_id=share.id, at=now
        ),
    )


async def _share_group(source: DataObject, *, owner: str) -> list[DataObject]:
    """Source, its sidecars, its mate, and the mate's sidecars -- deduplicated,
    source first so the parent id exists before a sidecar needs to point at it.

    Both cascades exist for the same reason `delete_object` cascades sidecars
    on the way out: a sidecar or a mate is not independently useful. Sharing a
    BAM without its BAI hands over a file most tools refuse to open, and
    sharing one half of a pair produces a file the pairing UI reports as half
    of a pair that does not exist.
    """
    group: list[DataObject] = [source]
    seen: set[PydanticObjectId] = {source.id}

    def _add(obj: DataObject) -> None:
        if obj.id not in seen:
            seen.add(obj.id)
            group.append(obj)

    for sidecar in await object_service.list_sidecars(source.id, owner=owner):
        _add(sidecar)

    if source.mate_object_id is not None:
        mate = await DataObject.get(source.mate_object_id)
        if mate is not None and mate.owner == owner:
            _add(mate)
            for sidecar in await object_service.list_sidecars(mate.id, owner=owner):
                _add(sidecar)

    return group


async def accept_share(
    *,
    owner: str,
    share_id: PydanticObjectId,
    project_id: PydanticObjectId | None = None,
) -> DataObject:
    """Materialize a shared file into the recipient's library.

    `project_id` is the recipient's choice of destination; omitted, the copy
    lands in a lazily created "Shared with me" project.

    The whole group -- parent, sidecars, mate -- is inserted and re-pointed as
    one transaction. A cascade that creates the BAM and then fails before its
    BAI leaves the recipient holding a broken file beside a *correct* blob
    ledger, which is the worst combination to debug: nothing is inconsistent
    enough for any check to notice.
    """
    share = await load_for_recipient(share_id, owner=owner)
    if share.state is not ShareState.OFFERED:
        raise ConflictError(
            f"This share is already {share.state.value}",
            details={"state": share.state.value},
        )

    source = await DataObject.get(share.source_object_id)
    if source is None or source.owner != share.from_owner:
        raise ConflictError("The sender deleted this file before it was accepted")

    if project_id is None:
        project = await _shared_with_me_project(owner)
    else:
        project = await project_service.get_project(project_id, owner=owner)  # 404 for others

    group = await _share_group(source, owner=share.from_owner)  # source is group[0]

    now = datetime.now(UTC)
    copies = [
        _copy_for(obj, owner=owner, project_id=project.id, share=share, now=now)
        for obj in group
    ]
    source_copy = copies[0]
    # For repointing sidecar_of/mate_object_id below, which name an arbitrary
    # id within the group -- not just the source's.
    source_id_to_copy = {src.id: copy for src, copy in zip(group, copies, strict=True)}

    async with await get_client().start_session() as session:
        async with session.start_transaction():
            for copy in copies:
                await copy.insert(session=session)

            # Re-point intra-group pointers at the new copies now that ids exist.
            for src, copy in zip(group, copies, strict=True):
                updates: dict = {}
                if src.sidecar_of is not None and src.sidecar_of in source_id_to_copy:
                    updates["sidecar_of"] = source_id_to_copy[src.sidecar_of].id
                if src.mate_object_id is not None and src.mate_object_id in source_id_to_copy:
                    updates["mate_object_id"] = source_id_to_copy[src.mate_object_id].id
                if updates:
                    await copy.set(updates, session=session)

            for src, copy in zip(group, copies, strict=True):
                if not src.blob_sha256:
                    continue
                await blob_service.attach_existing_blob_to_object(
                    object_id=copy.id,
                    digest=src.blob_sha256,
                    size=src.size,
                    session=session,
                )

            share.state = ShareState.ACCEPTED
            share.accepted_object_id = source_copy.id
            await share.save(session=session)

    await project_service.bump_counters(
        project.id, objects=len(copies), total_bytes=sum(c.size for c in copies)
    )

    # Not built here, deliberately -- see #51 (owner-delete/GC follow-on):
    #
    # Report directories (qc_reports/, bam_stats/, vcf_stats/) are keyed by
    # object id, not digest, so the copies above have none. A fallback through
    # `shared_from.object_id` is the wrong fix: it breaks the moment the sender
    # deletes their copy, since delete_object removes those directories. #51
    # copies the report directory at share time instead.

    return await DataObject.get(source_copy.id)  # type: ignore[return-value]
