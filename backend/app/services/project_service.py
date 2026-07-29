"""Project CRUD and breadcrumb construction."""

from datetime import UTC, datetime

from beanie import PydanticObjectId
from pymongo.errors import DuplicateKeyError
from slugify import slugify

from app.db.client import get_db
from app.errors import ConflictError, NotFoundError, ValidationError
from app.logging import get_logger
from app.models import DataObject, Project

log = get_logger(__name__)


async def create_project(
    *,
    name: str,
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
        if parent is None:
            raise NotFoundError(f"Parent project not found: {parent_id}")
        path = [*parent.path, parent.id]

    project = Project(
        name=name,
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


async def get_project(project_id: PydanticObjectId) -> Project:
    project = await Project.get(project_id)
    if project is None:
        raise NotFoundError(f"Project not found: {project_id}")
    return project


async def list_projects(
    *,
    parent_id: PydanticObjectId | None = None,
    include_archived: bool = False,
    limit: int = 200,
) -> list[Project]:
    query: dict = {"parent_id": parent_id}
    if not include_archived:
        query["archived"] = False
    return await Project.find(query).sort("-updated_at").limit(limit).to_list()


async def breadcrumbs(project: Project) -> list[dict]:
    """Resolve the materialized ancestor path into display entries.

    One query for the whole chain rather than a walk up the tree.
    """
    if not project.path:
        return [{"id": str(project.id), "name": project.name}]

    ancestors = await Project.find({"_id": {"$in": project.path}}).to_list()
    by_id = {a.id: a for a in ancestors}
    trail = [
        {"id": str(pid), "name": by_id[pid].name} for pid in project.path if pid in by_id
    ]
    trail.append({"id": str(project.id), "name": project.name})
    return trail


async def update_project(project_id: PydanticObjectId, updates: dict) -> Project:
    project = await get_project(project_id)

    if "name" in updates and updates["name"] is not None:
        new_name = updates["name"].strip()
        if not new_name:
            raise ValidationError("Project name cannot be empty")
        project.name = new_name
        project.slug = slugify(new_name)
    for field in ("description", "tags", "archived"):
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


async def delete_project(project_id: PydanticObjectId, *, cascade: bool = False) -> None:
    project = await get_project(project_id)

    object_count = await DataObject.find(DataObject.project_id == project_id).count()
    child_count = await Project.find(Project.parent_id == project_id).count()

    if not cascade and (object_count or child_count):
        raise ConflictError(
            "Project is not empty. Delete its contents first, or pass cascade=true.",
            details={"object_count": object_count, "child_project_count": child_count},
        )

    if cascade:
        from app.services import blob_service

        # Detach one at a time so each refcount decrement is transactional.
        for obj in await DataObject.find(DataObject.project_id == project_id).to_list():
            await blob_service.detach_blob_from_object(obj.id)
        for child in await Project.find(Project.parent_id == project_id).to_list():
            await delete_project(child.id, cascade=True)

    await project.delete()


async def collect_subtree(project_id: PydanticObjectId) -> list[PydanticObjectId]:
    """Every project in this subtree, root first, then breadth-first.

    Both the deletion preview and the delete itself build on this, so the two
    cannot disagree about what "this project" covers -- a warning that
    undercounts what is about to be destroyed is worse than no warning.
    """
    found = [project_id]
    frontier = [project_id]
    while frontier:
        children = await Project.find({"parent_id": {"$in": frontier}}).to_list()
        frontier = [c.id for c in children]
        found.extend(frontier)
    return found


async def deletion_preview(project_id: PydanticObjectId) -> dict:
    """What deleting this project would destroy, and whether it may proceed.

    Computed from collect_subtree so the numbers shown in the confirmation
    match what the delete actually removes.
    """
    from app.models import Job, PipelineRun, UploadSession
    from app.models.job import ACTIVE_STATES

    await get_project(project_id)
    ids = await collect_subtree(project_id)

    objects = await DataObject.find({"project_id": {"$in": ids}}).to_list()
    active = await Job.find(
        {"project_id": {"$in": ids}, "state": {"$in": [s.value for s in ACTIVE_STATES]}}
    ).to_list()

    return {
        "project_ids": [str(i) for i in ids],
        "child_project_count": len(ids) - 1,
        "object_count": len(objects),
        # Bytes *referenced*, not bytes that will be freed: a blob shared with
        # an object outside this subtree stays on disk.
        "total_bytes": sum(o.size for o in objects),
        "run_count": await PipelineRun.find({"project_id": {"$in": ids}}).count(),
        "job_count": await Job.find({"project_id": {"$in": ids}}).count(),
        "upload_session_count": await UploadSession.find(
            {"project_id": {"$in": ids}}
        ).count(),
        "active_jobs": [
            {"id": str(j.id), "job_type": j.type, "state": j.state.value} for j in active
        ],
        "blocked": bool(active),
    }


async def delete_project_tree(project_id: PydanticObjectId) -> dict:
    """Delete a project and everything belonging to it.

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

    preview = await deletion_preview(project_id)
    if preview["blocked"]:
        raise ConflictError(
            "Project has jobs that are still active. Wait for them to finish, "
            "or cancel them, then try again.",
            details={"active_jobs": preview["active_jobs"]},
        )

    ids = [PydanticObjectId(i) for i in preview["project_ids"]]

    # Deepest first: if this fails partway, what remains is a valid tree rather
    # than orphans pointing at a parent that no longer exists.
    for pid in reversed(ids):
        for obj in await DataObject.find(DataObject.project_id == pid).to_list():
            # A sidecar may already be gone, cascaded with its parent above.
            if await DataObject.get(obj.id) is not None:
                await object_service.delete_object(obj.id)

        for session in await UploadSession.find(
            UploadSession.project_id == pid
        ).to_list():
            await upload_service.abort_session(session.id)
            await session.delete()

        for run in await PipelineRun.find(PipelineRun.project_id == pid).to_list():
            await RunJob.find(RunJob.run_id == run.id).delete()
            await run.delete()

        # Unlike discard_run, this does not spare shared jobs: a build_index
        # job is deduped by blob digest and owned by whichever project queued
        # it first (see _enqueue_build_index in pipeline_service.py), so
        # deleting that owner here also deletes a job a wholly separate
        # project's RunJob still points at (linked with shared=True). That's
        # a silent cross-project side effect, not corruption -- run_detail
        # already renders a missing job as "expired" rather than erroring.
        await Job.find(Job.project_id == pid).delete()

        project = await Project.get(pid)
        if project is not None:
            await project.delete()

    log.info(
        "project_tree_deleted",
        project_id=str(project_id),
        projects=len(ids),
        objects=preview["object_count"],
    )
    return preview


async def bump_counters(project_id: PydanticObjectId, *, objects: int, total_bytes: int) -> None:
    """Adjust denormalized rollups. Used outside the transactional paths."""
    await get_db().projects.update_one(
        {"_id": project_id},
        {
            "$inc": {"counters.object_count": objects, "counters.total_bytes": total_bytes},
            "$set": {"updated_at": datetime.now(UTC)},
        },
    )
