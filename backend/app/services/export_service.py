"""Export one project and its descendants to a shareable archive.

The archive documents an analysis for a collaborator to read, check, or
cite. It is not a backup (ops/backup.sh is), and it is not currently
importable -- but it carries a version stamp and preserves ObjectIds so
that an importer stays possible later.

`Share`/`share_service.py` is deliberately not reused: it is
profile-to-profile on one machine and moves no bytes by design, both sides
pointing at the same refcounted blob. Crossing machines is precisely what
it cannot do.

See docs/superpowers/specs/2026-08-17-project-export-archive-design.md.
"""

from dataclasses import dataclass, field

from beanie import PydanticObjectId

from app.errors import NotFoundError
from app.models import (
    Blob,
    DataObject,
    JobRunTiming,
    PipelineRun,
    Project,
    RunJob,
)
from app.services import project_service

# Bumped when the archive layout changes in a way a reader must notice.
# Preserved ObjectIds plus this stamp are what a future importer needs.
BIOFLOW_EXPORT_VERSION = 1

# Blobs at or below this size have their bytes packed into the archive;
# larger ones are listed in the manifest as excluded. A collaborator wants
# the derived results, not hundreds of gigabytes of FASTQ.
DEFAULT_BLOB_THRESHOLD_BYTES = 100 * 1024 * 1024


@dataclass
class ExportBundle:
    """Everything in scope for one export, before redaction."""

    projects: list[Project] = field(default_factory=list)
    objects: list[DataObject] = field(default_factory=list)
    runs: list[PipelineRun] = field(default_factory=list)
    run_jobs: list[RunJob] = field(default_factory=list)
    timings: list[JobRunTiming] = field(default_factory=list)
    blobs: list[Blob] = field(default_factory=list)


async def collect(project_id: PydanticObjectId, *, owner: str) -> ExportBundle:
    """Gather one project, its descendants, and everything they reference.

    Descendants come from `project_service.collect_subtree`, which walks
    `Project.parent_id` breadth-first -- the same helper the deletion
    preview and cascade use, so an export's notion of "this project" never
    disagrees with theirs.

    Owner-scoped throughout: an export must never reach into another
    profile's partition, and the root lookup is what stands between a
    request and someone else's project.
    """
    root = await Project.find_one(Project.id == project_id, Project.owner == owner)
    if root is None:
        raise NotFoundError(f"Project {project_id} not found")

    project_ids = await project_service.collect_subtree(project_id, owner=owner)
    projects = await Project.find({"_id": {"$in": project_ids}}).to_list()

    objects = await DataObject.find(
        DataObject.owner == owner, {"project_id": {"$in": project_ids}}
    ).to_list()
    runs = await PipelineRun.find({"project_id": {"$in": project_ids}}).to_list()
    run_ids = [r.id for r in runs]
    run_jobs = await RunJob.find({"run_id": {"$in": run_ids}}).to_list() if run_ids else []

    blob_ids = sorted({o.blob_sha256 for o in objects if o.blob_sha256 is not None})
    blobs = await Blob.find({"_id": {"$in": blob_ids}}).to_list() if blob_ids else []

    object_ids = [str(o.id) for o in objects]
    timings = (
        await JobRunTiming.find({"object_id": {"$in": object_ids}}).to_list() if object_ids else []
    )

    return ExportBundle(
        projects=projects,
        objects=objects,
        runs=runs,
        run_jobs=run_jobs,
        timings=timings,
        blobs=blobs,
    )
