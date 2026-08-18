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

import asyncio
import hashlib
import io
import json
import tarfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from beanie import PydanticObjectId

from app.config import settings
from app.errors import NotFoundError
from app.models import (
    Blob,
    DataObject,
    JobRunTiming,
    PipelineRun,
    Project,
    RunJob,
)
from app.services import project_service, run_service

# Bumped when the archive layout changes in a way a reader must notice.
# Preserved ObjectIds plus this stamp are what a future importer needs.
BIOFLOW_EXPORT_VERSION = 1

# Blobs at or below this size have their bytes packed into the archive;
# larger ones are listed in the manifest as excluded. A collaborator wants
# the derived results, not hundreds of gigabytes of FASTQ.
DEFAULT_BLOB_THRESHOLD_BYTES = 100 * 1024 * 1024


REPORT_ARTIFACT_ROOTS: tuple[tuple[str, str], ...] = (
    ("qc", "qc_reports_dir"),
    ("bam_stats", "bam_stats_dir"),
    ("vcf_stats", "vcf_stats_dir"),
    ("annotation_stats", "annotation_stats_dir"),
)


@dataclass
class ExportArtifact:
    artifact_type: str
    artifact_id: str
    category: str
    source_path: str
    archive_path: str
    size: int
    sha256: str
    status: str = "present"
    object_id: str | None = None
    state: str | None = None


@dataclass
class ExportBundle:
    """Everything in scope for one export, before redaction.

    `root` is the project the export was requested for -- set explicitly
    from `collect()`'s own lookup rather than inferred as `projects[0]`,
    because `projects` is populated via a Mongo `$in` query and `$in` does
    not preserve array order. `projects` holds the full unordered set
    (root plus descendants) for anything that iterates all of them
    regardless of order.
    """

    root: Project | None = None
    projects: list[Project] = field(default_factory=list)
    objects: list[DataObject] = field(default_factory=list)
    runs: list[PipelineRun] = field(default_factory=list)
    run_jobs: list[RunJob] = field(default_factory=list)
    timings: list[JobRunTiming] = field(default_factory=list)
    blobs: list[Blob] = field(default_factory=list)
    report_artifacts: list[ExportArtifact] = field(default_factory=list)


def _hash_file(path: Path) -> tuple[int, str]:
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while chunk := fh.read(64 * 1024):
            size += len(chunk)
            hasher.update(chunk)
    return size, hasher.hexdigest()


def collect_report_artifacts(objects: list[DataObject]) -> list[ExportArtifact]:
    """Collect report files written beside objects, not in the blob store."""
    artifacts: list[ExportArtifact] = []

    for obj in objects:
        object_id = str(obj.id)
        for category, settings_attr in REPORT_ARTIFACT_ROOTS:
            root = getattr(settings, settings_attr)
            object_dir = root / object_id
            if not object_dir.exists() or not object_dir.is_dir():
                continue

            resolved_object_dir = object_dir.resolve()
            for path in object_dir.rglob("*"):
                if path.is_symlink() or not path.is_file():
                    continue

                try:
                    resolved = path.resolve()
                    resolved.relative_to(resolved_object_dir)
                    size, sha256 = _hash_file(path)
                except (OSError, ValueError):
                    continue

                source_path = path.relative_to(object_dir).as_posix()
                artifacts.append(
                    ExportArtifact(
                        artifact_type="report",
                        artifact_id=f"{category}:{object_id}:{source_path}",
                        object_id=object_id,
                        category=category,
                        source_path=source_path,
                        archive_path=f"reports/{category}/{object_id}/{source_path}",
                        size=size,
                        sha256=sha256,
                    )
                )

    return sorted(
        artifacts,
        key=lambda artifact: (
            artifact.category,
            artifact.object_id or "",
            artifact.source_path,
        ),
    )


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
    report_artifacts = await asyncio.to_thread(collect_report_artifacts, objects)

    # Timings are keyed by object *or* by project: `executor.py` records
    # `object_id` only for a job that attached to a single object, so
    # project-level work (and any job whose object link was never recorded)
    # carries a null `object_id` and only `project_id`. Querying on
    # `object_id` alone silently drops those, and the archive has no way to
    # say a run happened but its timing was left out -- the gap reads as
    # "this project ran nothing".
    object_ids = [str(o.id) for o in objects]
    timing_keys: list[dict] = [{"project_id": {"$in": [str(p) for p in project_ids]}}]
    if object_ids:
        timing_keys.append({"object_id": {"$in": object_ids}})
    timings = await JobRunTiming.find({"$or": timing_keys}).to_list()

    return ExportBundle(
        root=root,
        projects=projects,
        objects=objects,
        runs=runs,
        run_jobs=run_jobs,
        timings=timings,
        blobs=blobs,
        report_artifacts=report_artifacts,
    )


# The collections an archive may contain, named explicitly.
#
# Exclusion by construction, and deliberately the OPPOSITE of ops/backup.sh,
# which dumps every collection with no allowlist. The two are right for
# opposite reasons: for a backup, a missed collection is silent permanent
# data loss, so including by default fails safe. For an export, a collection
# added later that holds something sensitive would quietly leave the machine,
# so excluding by default fails safe.
#
# Adding a collection here is a decision to send its contents to someone
# else. `ai_providers`, `app_settings`, `nodes`, and `profiles` are absent
# and must stay absent.
SERIALIZED_COLLECTIONS = (
    "projects",
    "objects",
    "runs",
    "run_jobs",
    "job_timings",
    "blobs",
)


@dataclass
class RedactionSummary:
    """What redaction removed, reported to the user after the fact."""

    paths_relativized: int = 0
    machine_records_cleared: int = 0
    profile: str = "secrets+paths+machine"


def _strip_paths(doc: dict) -> int:
    """Drop absolute filesystem paths. Returns how many were removed.

    `rel_path` survives: it is relative by construction and the manifest
    needs it. The two real path-bearing fields across the redacted
    collections (`Blob`, `DataObject`, `PipelineRun`, `RunJob`,
    `JobRunTiming`) are `Blob.external_path` (top-level) and
    `DataObject.source.original_path` (nested under `SourceInfo`, set for
    every register-in-place object -- see object_service.py). Both leak a
    username and directory layout, and mean nothing on the recipient's
    machine.
    """
    removed = 0
    if doc.get("external_path") is not None:
        doc["external_path"] = None
        removed += 1
    source = doc.get("source")
    if isinstance(source, dict) and source.get("original_path") is not None:
        source["original_path"] = None
        removed += 1
    return removed


def redact(bundle: ExportBundle) -> tuple[dict[str, list[dict]], RedactionSummary]:
    """Serialize the bundle, stripping secrets, paths, and machine identity.

    Returns the per-collection documents and a summary of what was removed,
    which the job reports so the user can check what left the machine.
    """
    summary = RedactionSummary()
    docs: dict[str, list[dict]] = {name: [] for name in SERIALIZED_COLLECTIONS}

    for project in bundle.projects:
        docs["projects"].append(project.model_dump(mode="json", by_alias=True))
    for obj in bundle.objects:
        d = obj.model_dump(mode="json", by_alias=True)
        summary.paths_relativized += _strip_paths(d)
        docs["objects"].append(d)
    for run in bundle.runs:
        docs["runs"].append(run.model_dump(mode="json", by_alias=True))
    for run_job in bundle.run_jobs:
        docs["run_jobs"].append(run_job.model_dump(mode="json", by_alias=True))
    for timing in bundle.timings:
        d = timing.model_dump(mode="json", by_alias=True)
        # Durations stay -- "this alignment took 40 minutes" is part of the
        # analysis record. "It ran on gio-workstation.local" is not.
        #
        # A default/unset RunMachine still dumps to a dict of all-None
        # values, which is truthy -- so this checks for an actual populated
        # field, not just the dict's presence, or every timing (even ones
        # that never recorded machine identity) would count as "cleared".
        machine = d.get("machine") or {}
        if any(v is not None for v in machine.values()):
            d["machine"] = {}
            # worker_id defaults to f"{socket.gethostname()}:{os.getpid()}"
            # (queue/worker.py) -- it is the same machine-identity fact as
            # `machine`, just carried in a sibling field, so it is cleared
            # under the same condition rather than unconditionally (an
            # unset worker_id counting as "cleared" would be the same
            # false-positive the `machine` check above already guards
            # against).
            d["worker_id"] = None
            summary.machine_records_cleared += 1
        docs["job_timings"].append(d)
    for blob in bundle.blobs:
        d = blob.model_dump(mode="json", by_alias=True)
        summary.paths_relativized += _strip_paths(d)
        docs["blobs"].append(d)

    return docs, summary


_MANIFEST_HEADER = (
    "blob_id",
    "size",
    "content_sha256",
    "state",
    "rel_path",
    "bytes",
)


def build_manifest(bundle: ExportBundle, *, threshold_bytes: int) -> tuple[str, list[Blob]]:
    """Render data-manifest.tsv and decide which blobs' bytes to pack.

    Every blob in scope gets a row, including those whose bytes are left
    out -- the last column says which. That distinction is the manifest's
    whole value: the recipient can tell "not sent" from "does not exist",
    and knows exactly what to ask for.

    Written as TSV, readable with cut and grep on a machine with no Mongo,
    no Docker, and no BioFlow. The recipient is the one person guaranteed
    not to have the app. Same shape as ops/backup.sh's manifest.
    """
    lines = ["\t".join(_MANIFEST_HEADER)]
    included: list[Blob] = []

    for blob in sorted(bundle.blobs, key=lambda b: str(b.id)):
        pack = blob.size <= threshold_bytes and blob.rel_path is not None
        if pack:
            included.append(blob)
        lines.append(
            "\t".join(
                (
                    str(blob.id),
                    str(blob.size),
                    # content_sha256 is only set when it differs from id (the
                    # compressed-blob case) -- for the common case (an
                    # uncompressed blob, or one predating compression) it is
                    # None, and id is already the canonical stored-bytes hash.
                    blob.content_sha256 or blob.id,
                    str(blob.state),
                    blob.rel_path or "",
                    "included" if pack else "excluded",
                )
            )
        )

    return "\n".join(lines) + "\n", included


async def render_report(bundle: ExportBundle, *, owner: str) -> str:
    """Render report.md: the analysis, readable without BioFlow.

    Per-object provenance comes from `provenance_report.render_markdown`,
    the same renderer the History tab uses, rather than a second one. That
    is deliberate: it carries the renderer's gap markers into the archive,
    so a reader scanning for "which aligner version" sees the question
    asked and unanswered rather than seeing nothing and assuming it did not
    matter.

    Run status is derived through `run_service.status_for_many` rather than
    read off `PipelineRun` directly -- `PipelineRun` stores no status field
    of its own; it is always computed from the run's linked jobs, the same
    way the activity view computes it.
    """
    from app.services import provenance_report, provenance_walker

    root = bundle.root
    lines = [
        f"# {root.name}",
        "",
        "_Exported from BioFlow. This archive documents an analysis; it "
        "cannot be imported into another BioFlow instance._",
        "",
    ]
    if root.description:
        lines += [root.description, ""]

    sub_projects = [p for p in bundle.projects if p.id != root.id]
    if sub_projects:
        lines += ["## Sub-projects", ""]
        lines += [f"- {p.name}" for p in sub_projects]
        lines.append("")

    lines += ["## Files", ""]
    for obj in bundle.objects:
        lines += [f"### {obj.name}", ""]
        chain = await provenance_walker.walk(obj.id, owner=owner)
        lines += [provenance_report.render_markdown(chain), ""]

    if bundle.runs:
        statuses = await run_service.status_for_many(
            [run.id for run in bundle.runs], owner=owner
        )
        lines += ["## Run history", ""]
        for run in bundle.runs:
            status = statuses.get(run.id, "unknown")
            lines.append(f"- {run.label} — {run.kind} — {status} ({run.created_at:%Y-%m-%d})")
        lines.append("")

    return "\n".join(lines)


_README = """# BioFlow project export

This archive documents an analysis: what was run, on what, with which
versions and parameters, producing which results.

## What it is

- `report.md` — the analysis in prose, readable without BioFlow.
- `data-manifest.tsv` — every file in the project, whether or not its bytes
  are in this archive. Readable with `cut` and `grep`.
- `metadata/` — the underlying records as JSON.
- `blobs/` — the bytes of files small enough to include.

## What it is not

**This archive cannot be imported into BioFlow.** It is a record to read,
check, and cite, not a project you can load. It carries a format version
and preserves record identity so that an importer remains possible later.

**Report and analysis-artifact directories are not included.** QC reports,
BAM/VCF stats, and annotation stats directories (`qc_reports_dir`,
`bam_stats_dir`, `vcf_stats_dir`, `annotation_stats_dir`) live outside the
blob store, keyed by object id, and this archive does not currently pack
them -- even though the objects they describe are exported. A recipient may
see an object without the report that made it easy to read. This is a
known, deliberate gap for a follow-up, not an oversight.

## What was removed

API keys, encryption keys, absolute filesystem paths, and the names of the
machines the analysis ran on. Durations and tool versions are kept —
they are part of the analysis.
"""


@dataclass
class ExportResult:
    path: Path
    size_bytes: int
    blob_count: int
    included_blob_count: int
    redaction: RedactionSummary


def _write_archive(
    dest: Path,
    *,
    docs: dict[str, list[dict]],
    manifest_json: dict,
    manifest_tsv: str,
    report: str,
    included: list[Blob],
) -> None:
    """Pack the tarball. Sync, called via asyncio.to_thread."""
    from app.storage.paths import blob_path

    def _add_bytes(tar: tarfile.TarFile, name: str, payload: bytes) -> None:
        info = tarfile.TarInfo(name)
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))

    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, "w:gz") as tar:
        _add_bytes(tar, "manifest.json",
                   json.dumps(manifest_json, indent=2).encode())
        _add_bytes(tar, "data-manifest.tsv", manifest_tsv.encode())
        _add_bytes(tar, "report.md", report.encode())
        _add_bytes(tar, "README.md", _README.encode())
        for name, rows in docs.items():
            _add_bytes(tar, f"metadata/{name}.json",
                       json.dumps(rows, indent=2).encode())
        for blob in included:
            # blob.id is the sha256 of the STORED bytes and is always set (it
            # is the primary key); blob.content_sha256 is only set when it
            # differs from id (the compressed-blob case), so it is None for
            # the common uncompressed blob. blob_path() resolves by the
            # digest of the bytes actually on disk, which is always blob.id
            # -- using content_sha256 here would silently skip packing bytes
            # for every uncompressed blob even though the manifest lists it
            # as included.
            src = blob_path(blob.id)
            if src.exists():
                tar.add(src, arcname=f"blobs/{blob.id}")


async def export_project(
    project_id: PydanticObjectId,
    *,
    owner: str,
    threshold_bytes: int = DEFAULT_BLOB_THRESHOLD_BYTES,
) -> ExportResult:
    """Produce one archive for a project and its descendants."""
    bundle = await collect(project_id, owner=owner)
    docs, redaction = redact(bundle)
    manifest_tsv, included = build_manifest(bundle, threshold_bytes=threshold_bytes)
    report = await render_report(bundle, owner=owner)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dest = settings.exports_dir / f"{owner}__{bundle.root.slug}-{stamp}.tar.gz"

    manifest_json = {
        "bioflow_export_version": BIOFLOW_EXPORT_VERSION,
        "exported_at": datetime.now(UTC).isoformat(),
        "project_id": str(project_id),
        "project_name": bundle.root.name,
        "project_count": len(bundle.projects),
        "counts": {name: len(rows) for name, rows in docs.items()},
        "blob_count": len(bundle.blobs),
        "included_blob_count": len(included),
        "blob_threshold_bytes": threshold_bytes,
        "redaction_profile": redaction.profile,
    }

    await asyncio.to_thread(
        _write_archive,
        dest,
        docs=docs,
        manifest_json=manifest_json,
        manifest_tsv=manifest_tsv,
        report=report,
        included=included,
    )

    return ExportResult(
        path=dest,
        size_bytes=dest.stat().st_size,
        blob_count=len(bundle.blobs),
        included_blob_count=len(included),
        redaction=redaction,
    )
