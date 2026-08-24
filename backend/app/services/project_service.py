"""Project CRUD and breadcrumb construction."""

from datetime import UTC, datetime

from beanie import PydanticObjectId
from pymongo.errors import DuplicateKeyError
from slugify import slugify

from app.db.client import get_db
from app.errors import ConflictError, NotFoundError, ValidationError
from app.logging import get_logger
from app.models import DataObject, Job, Project

log = get_logger(__name__)


async def create_project(
    *,
    name: str,
    owner: str,
    description: str = "",
    parent_id: PydanticObjectId | None = None,
    metadata: dict | None = None,
    tags: list[str] | None = None,
) -> Project:
    name = name.strip()
    if not name:
        raise ValidationError("Project name cannot be empty")

    path: list[PydanticObjectId] = []
    if parent_id is not None:
        parent = await Project.get(parent_id)
        # Another profile's project is "not found" here, exactly as a missing
        # one is -- otherwise a nested create would quietly build a tree that
        # straddles the partition, and the child's `path` would name ancestors
        # its owner can never see.
        if parent is None or parent.owner != owner:
            raise NotFoundError(f"Parent project not found: {parent_id}")
        path = [*parent.path, parent.id]

    project = Project(
        name=name,
        owner=owner,
        slug=slugify(name),
        description=description,
        parent_id=parent_id,
        path=path,
        metadata=metadata or {},
        tags=tags or [],
    )
    try:
        await project.insert()
    except DuplicateKeyError as e:
        raise ConflictError(
            f"A project named {name!r} already exists here",
            details={"name": name, "parent_id": str(parent_id) if parent_id else None},
        ) from e
    return project


async def get_project(project_id: PydanticObjectId, *, owner: str) -> Project:
    """Fetch a project, scoped to its owner.

    A wrong-owner lookup raises the same NotFoundError as a missing one, on
    purpose: it keeps every existing caller's error handling working unchanged,
    and it does not confirm to one profile that another profile's id exists.
    """
    project = await Project.get(project_id)
    if project is None or project.owner != owner:
        raise NotFoundError(f"Project not found: {project_id}")
    return project


async def list_projects(
    *,
    owner: str,
    parent_id: PydanticObjectId | None = None,
    include_archived: bool = False,
    limit: int = 200,
) -> list[Project]:
    query: dict = {"owner": owner, "parent_id": parent_id}
    if not include_archived:
        query["archived"] = False
    return await Project.find(query).sort("-updated_at").limit(limit).to_list()


async def breadcrumbs(project: Project, *, owner: str) -> list[dict]:
    """Resolve the materialized ancestor path into display entries.

    One query for the whole chain rather than a walk up the tree.

    `project` arrives already fetched, so this cannot re-check that the caller
    was entitled to it -- the owner filter here only governs which ancestor
    *names* get resolved. Callers must have obtained `project` through
    get_project.
    """
    if not project.path:
        return [{"id": str(project.id), "name": project.name}]

    ancestors = await Project.find({"owner": owner, "_id": {"$in": project.path}}).to_list()
    by_id = {a.id: a for a in ancestors}
    trail = [
        {"id": str(pid), "name": by_id[pid].name} for pid in project.path if pid in by_id
    ]
    trail.append({"id": str(project.id), "name": project.name})
    return trail


async def update_project(
    project_id: PydanticObjectId, updates: dict, *, owner: str
) -> Project:
    project = await get_project(project_id, owner=owner)

    if "name" in updates and updates["name"] is not None:
        new_name = updates["name"].strip()
        if not new_name:
            raise ValidationError("Project name cannot be empty")
        project.name = new_name
        project.slug = slugify(new_name)
    for field in ("description", "tags", "archived", "agent_system_prompt"):
        if updates.get(field) is not None:
            setattr(project, field, updates[field])
    if updates.get("metadata") is not None:
        # Merge rather than replace, so a partial edit cannot drop other keys.
        project.metadata = {**project.metadata, **updates["metadata"]}

    project.touch()
    try:
        await project.save()
    except DuplicateKeyError as e:
        raise ConflictError(f"A project named {project.name!r} already exists here") from e
    return project


async def delete_project(
    project_id: PydanticObjectId, *, owner: str, cascade: bool = False
) -> None:
    """Delete a project, optionally with everything inside it.

    `cascade=True` delegates to delete_project_tree rather than detaching blobs
    itself. The hand-rolled loop this replaces skipped object_service's sidecar
    cascade and stranded index blobs at refcount 1, where GC could never reach
    them.
    """
    project = await get_project(project_id, owner=owner)

    object_count = await DataObject.find(
        DataObject.project_id == project_id, DataObject.owner == owner
    ).count()
    child_count = await Project.find(
        Project.parent_id == project_id, Project.owner == owner
    ).count()

    if not cascade and (object_count or child_count):
        # Worded for a person, because it reaches one: the API detail is in
        # `details` and the docs, but this string is what the UI surfaces in a
        # toast. Telling a user to "pass cascade=true" names a query parameter
        # they have no way to set from the app.
        raise ConflictError(
            "This project still has contents. Deleting it will also delete "
            "everything inside it.",
            details={"object_count": object_count, "child_project_count": child_count},
        )

    if cascade:
        await delete_project_tree(project_id, owner=owner)
        return

    await project.delete()


async def collect_subtree(
    project_id: PydanticObjectId, *, owner: str
) -> list[PydanticObjectId]:
    """Every project in this subtree, root first, then breadth-first.

    Both the deletion preview and the delete itself build on this, so the two
    cannot disagree about what "this project" covers -- a warning that
    undercounts what is about to be destroyed is worse than no warning.

    The owner filter is not merely a read scope here: what this returns is what
    the delete destroys, so an unscoped descendant would be another profile's
    project deleted without ever appearing in its owner's preview.
    """
    found = [project_id]
    frontier = [project_id]
    while frontier:
        children = await Project.find(
            {"owner": owner, "parent_id": {"$in": frontier}}
        ).to_list()
        frontier = [c.id for c in children]
        found.extend(frontier)
    return found


async def deletion_preview(project_id: PydanticObjectId, *, owner: str) -> dict:
    """What deleting this project would destroy, and whether it may proceed.

    Computed from collect_subtree so the numbers shown in the confirmation
    match what the delete actually removes.
    """
    from app.models import Job, PipelineRun, UploadSession
    from app.models.job import ACTIVE_STATES

    await get_project(project_id, owner=owner)
    ids = await collect_subtree(project_id, owner=owner)

    objects = await DataObject.find({"owner": owner, "project_id": {"$in": ids}}).to_list()
    active = await Job.find(
        {
            "owner": owner,
            "project_id": {"$in": ids},
            "state": {"$in": [s.value for s in ACTIVE_STATES]},
        }
    ).to_list()

    return {
        "project_ids": [str(i) for i in ids],
        "child_project_count": len(ids) - 1,
        "object_count": len(objects),
        # Bytes *referenced*, not bytes that will be freed: a blob shared with
        # an object outside this subtree stays on disk.
        "total_bytes": sum(o.size for o in objects),
        "run_count": await PipelineRun.find(
            {"owner": owner, "project_id": {"$in": ids}}
        ).count(),
        "job_count": await Job.find({"owner": owner, "project_id": {"$in": ids}}).count(),
        "upload_session_count": await UploadSession.find(
            {"owner": owner, "project_id": {"$in": ids}}
        ).count(),
        "active_jobs": [
            {"id": str(j.id), "job_type": j.type, "state": j.state.value} for j in active
        ],
        "blocked": bool(active),
    }


async def delete_project_tree(project_id: PydanticObjectId, *, owner: str) -> dict:
    """Delete a project and everything belonging to it.

    Ownership is settled before anything is destroyed: deletion_preview below
    calls get_project first, so a wrong-owner call raises NotFoundError while
    the tree is still intact rather than discovering its mistake partway
    through the delete loop.

    Delegates each object to object_service.delete_object rather than
    detaching blobs here. That delegation is load-bearing: delete_object
    cascades to sidecars, and a sidecar orphaned by its parent holds its blob
    at refcount 1 forever, where GC can never reach it.

    Bytes are not unlinked here. Refcounts reach zero and the grace-windowed
    gc_blobs job reclaims them later, which is also the manual-recovery window
    for a delete the user regrets.
    """
    from app.models import Job, PipelineRun, RunJob, UploadSession
    from app.services import object_service, upload_service

    preview = await deletion_preview(project_id, owner=owner)
    if preview["blocked"]:
        raise ConflictError(
            "Project has jobs that are still active. Wait for them to finish, "
            "or cancel them, then try again.",
            details={"active_jobs": preview["active_jobs"]},
        )

    ids = [PydanticObjectId(i) for i in preview["project_ids"]]

    # The DataObject filter below is live: object_service now stamps `owner` on
    # every object it creates, so a non-"local" project's objects really are
    # matched here and really are reclaimed. test_object_service_owner.py's
    # cascade test drives ingest_local_file end to end to hold that down.
    #
    # The PipelineRun filter is live too, as of Task 5: run_service.create_run
    # now stamps `owner` on every run it records, and each launch path passes
    # the owner of the project or the file the run operates on. A non-"local"
    # project's runs really are matched here and really are deleted.
    #
    # The Job filter is live as of Task 8: queue/queue.py's `enqueue` now takes
    # an owner and stamps it on every Job it constructs, and each call site
    # passes the owner of the object, project, or run it is acting on. A
    # non-"local" project's jobs really are matched here.
    #
    # The UploadSession filter is live too: upload_service.create_session now
    # takes an owner and stamps it on the session, so a non-"local" project's
    # sessions really are matched here and their staging directories really are
    # removed. Before that, every session carried the "local" default from
    # TimestampedDocument, and deleting a profile-owned project left the
    # session orphaned and its chunks on disk indefinitely.
    #
    # All four filters are now backed by a writer that sets `owner`. The
    # remaining gap is above the service layer: the routes in api/v1 still
    # hardcode owner="local" pending get_current_owner, so nothing yet produces
    # a non-"local" row in normal use.

    # Deepest first: if this fails partway, what remains is a valid tree rather
    # than orphans pointing at a parent that no longer exists.
    for pid in reversed(ids):
        for obj in await DataObject.find(
            DataObject.project_id == pid, DataObject.owner == owner
        ).to_list():
            # A sidecar may already be gone, cascaded with its parent above.
            if await DataObject.get(obj.id) is not None:
                await object_service.delete_object(obj.id, owner=owner)

        for session in await UploadSession.find(
            UploadSession.project_id == pid, UploadSession.owner == owner
        ).to_list():
            await upload_service.abort_session(session.id, owner=owner)
            await session.delete()

        for run in await PipelineRun.find(
            PipelineRun.project_id == pid, PipelineRun.owner == owner
        ).to_list():
            # Deliberately not owner-filtered. RunJob is a link row with no
            # owner of its own to speak of -- it is reached only through `run`,
            # which get_project and collect_subtree have already confirmed
            # belongs to this owner, so run_id is the whole scope. Adding an
            # owner filter would at best repeat a check already made, and at
            # worst strand link rows pointing at a run that no longer exists.
            await RunJob.find(RunJob.run_id == run.id).delete()
            await run.delete()

        # Unlike discard_run, this does not spare shared jobs: a build_index
        # job is deduped by blob digest and owned by whichever project queued
        # it first (see _enqueue_build_index in pipeline_service.py), so
        # deleting that owner here also deletes a job a wholly separate
        # project's RunJob still points at (linked with shared=True). That's
        # a silent cross-project side effect, not corruption -- run_detail
        # already renders a missing job as "expired" rather than erroring.
        await Job.find(Job.project_id == pid, Job.owner == owner).delete()

        project = await Project.get(pid)
        # Belt and braces, kept on purpose. collect_subtree should only have
        # returned this owner's projects, but this is the statement that
        # actually destroys a project document, and a bug upstream in the
        # traversal would spend itself here rather than on another profile's
        # tree. One comparison is a cheap place to stop being clever.
        if project is not None and project.owner == owner:
            await project.delete()

    log.info(
        "project_tree_deleted",
        project_id=str(project_id),
        projects=len(ids),
        objects=preview["object_count"],
    )
    return preview


async def bump_counters(project_id: PydanticObjectId, *, objects: int, total_bytes: int) -> None:
    """Adjust denormalized rollups. Used outside the transactional paths.

    The lone owner-less query on a partitioned collection, and deliberately so:
    every call site reaches here holding an object it has already resolved
    through an owner-scoped lookup, so the project id is pre-verified by the
    time it arrives. Counters are also derived rollups rather than readable
    data -- nothing here reveals or returns another profile's content -- so an
    owner parameter would be threading for symmetry without buying isolation.
    """
    await get_db().projects.update_one(
        {"_id": project_id},
        {
            "$inc": {"counters.object_count": objects, "counters.total_bytes": total_bytes},
            "$set": {"updated_at": datetime.now(UTC)},
        },
    )


async def recent_jobs(project_id: PydanticObjectId, *, limit: int = 5) -> list[dict]:
    """Fetch the most recent completed jobs for a project.

    Returns plain, JSON-serialisable summaries: this feeds the agent's
    spawn-time project context, which is assembled into prompt text, so a
    nested pydantic model here would fail well away from this function.
    `progress` is therefore the percentage, not the whole JobProgress.
    """
    jobs = (
        await Job.find(
            {"project_id": project_id, "state": {"$in": ["succeeded", "failed"]}}
        )
        .sort("-updated_at")
        .limit(limit)
        .to_list()
    )
    return [
        {
            "type": j.type,
            "state": j.state,
            "progress": j.progress.pct,
            "created_at": j.created_at.isoformat() if j.created_at else None,
        }
        for j in jobs
    ]
