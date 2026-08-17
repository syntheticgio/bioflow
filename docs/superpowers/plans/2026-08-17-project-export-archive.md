# Per-Project Export Archive Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Export one project and its descendants to a single `.tar.gz` archive that documents the analysis for a collaborator to read, check, or cite.

**Architecture:** A new `export_service.py` collects the project subtree, redacts it, renders a human-readable report through the existing `provenance_report.render_markdown`, and packs a tarball. A `THREAD`-mode queue handler runs it; a launcher in `pipeline_service.py` enqueues it and is registered in `EXCLUDED_LAUNCHES`. Three API endpoints create, list, and download. The UI adds a project-level action with a size-preview dialog.

**Tech Stack:** Python 3.12, FastAPI, Beanie/Motor (MongoDB), pytest, React + TypeScript (Vite), Docker Compose.

**Spec:** `docs/superpowers/specs/2026-08-17-project-export-archive-design.md`

## Global Constraints

- **Secrets never enter the archive.** No API-key ciphertext, no Fernet key, nothing from `.biopipe/`. Enforced by grep assertions over the produced archive, not by care.
- **Exclusion by construction.** The exporter names the collections it serializes. Never dump-and-subtract. This deliberately inverts #411's backup strategy — backup fails safe by including, export fails safe by excluding.
- **Redaction covers three tiers:** secrets (never present), filesystem paths (stripped/relativized — `external_path`, `BIOINFO_HOME`, user home paths; `rel_path` survives), machine and node identity (`JobRunTiming.machine` including `machine_id`, node records, SSH targets, machine profiles). Timing *durations* stay.
- **The manifest lists every blob in scope**, including those whose bytes are excluded, flagged as excluded. The recipient must be able to distinguish "not sent" from "does not exist."
- **Format version constant:** `BIOFLOW_EXPORT_VERSION = 1`. ObjectIds are preserved verbatim in `metadata/*.json` so a future importer has stable identity to remap.
- **Sidecars and report directories follow their object.** Not a separate opt-in; subject to the same size threshold.
- **Default size threshold:** 100 MB per blob, overridable per export.
- **Tests run via `./backend/run-worktree-tests.sh tests/ -q` from this worktree.** Never `docker compose exec api` — that silently tests main's code, not the worktree's.
- **Commit style:** Conventional Commits, imperative mood, lowercase after the colon, no trailing period. Scope `export` unless the change is elsewhere.

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/app/services/export_service.py` | Collect the project subtree, redact, render, pack. The whole export, one module. |
| `backend/app/services/pipeline_service.py` | `launch_project_export` — enqueue the job. |
| `backend/app/queue/pipeline_handlers.py` | `project_export` handler, `HandlerMode.THREAD`. |
| `backend/app/pipelines/node_types.py` | Add the launcher to `EXCLUDED_LAUNCHES` with a comment. |
| `backend/app/config.py` | `exports_dir` property. |
| `backend/app/api/v1/exports.py` | Create, list, download endpoints. |
| `backend/tests/services/test_export_service.py` | Collection, redaction greps, manifest, envelope. |
| `backend/tests/api/test_exports_api.py` | Endpoint behavior. |
| `frontend/src/` | Project-level export action and size-preview dialog. |

**Note on `HandlerMode`:** the spec said `SUBPROCESS`; that was wrong. `registry.py:36` documents SUBPROCESS as "spawns processes; killed by process group." This packs a tarball in-process, so it is `THREAD` — "sync, CPU/IO-bound; run via `asyncio.to_thread`" (`registry.py:35`). Corrected here and in the spec.

---

### Task 1: Export directory and format constants

**Files:**
- Modify: `backend/app/config.py` (after `logs_dir`, ~line 311)
- Create: `backend/app/services/export_service.py`
- Test: `backend/tests/services/test_export_service.py`

**Interfaces:**
- Consumes: `settings.bioinfo_home` from `app/config.py`
- Produces: `settings.exports_dir -> Path`, `export_service.BIOFLOW_EXPORT_VERSION: int`, `export_service.DEFAULT_BLOB_THRESHOLD_BYTES: int`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/services/test_export_service.py
from app.config import settings
from app.services import export_service


def test_exports_dir_is_under_bioinfo_home():
    assert settings.exports_dir == settings.bioinfo_home / "exports"


def test_export_format_constants():
    assert export_service.BIOFLOW_EXPORT_VERSION == 1
    assert export_service.DEFAULT_BLOB_THRESHOLD_BYTES == 100 * 1024 * 1024
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_export_service.py -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'exports_dir'`

- [ ] **Step 3: Write minimal implementation**

In `backend/app/config.py`, after the `logs_dir` property:

```python
    @property
    def exports_dir(self) -> Path:
        """Where project export archives land.

        Outside objects/ deliberately, same rationale as qc_reports_dir: an
        export is a derived artifact keyed by job, not a blob. Retention is
        the user's job -- automatic pruning is a feature whose bugs delete
        things the user meant to send.
        """
        return self.bioinfo_home / "exports"
```

Create `backend/app/services/export_service.py`:

```python
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

# Bumped when the archive layout changes in a way a reader must notice.
# Preserved ObjectIds plus this stamp are what a future importer needs.
BIOFLOW_EXPORT_VERSION = 1

# Blobs at or below this size have their bytes packed into the archive;
# larger ones are listed in the manifest as excluded. A collaborator wants
# the derived results, not hundreds of gigabytes of FASTQ.
DEFAULT_BLOB_THRESHOLD_BYTES = 100 * 1024 * 1024
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/services/test_export_service.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/config.py backend/app/services/export_service.py backend/tests/services/test_export_service.py
git commit -m "feat(export): add exports directory and archive format constants"
```

---

### Task 2: Collect the project subtree

**Files:**
- Modify: `backend/app/services/export_service.py`
- Test: `backend/tests/services/test_export_service.py`

**Interfaces:**
- Consumes: `Project`, `DataObject`, `PipelineRun`, `RunJob`, `Blob` from `app.models`; `BIOFLOW_EXPORT_VERSION` from Task 1
- Produces: `async def collect(project_id: PydanticObjectId, *, owner: str) -> ExportBundle`, and the `ExportBundle` dataclass with fields `projects: list[Project]`, `objects: list[DataObject]`, `runs: list[PipelineRun]`, `run_jobs: list[RunJob]`, `timings: list[JobRunTiming]`, `blobs: list[Blob]`

Descendants matter: `Project.path` is a list of ancestor ids, so every descendant of `P` has `P` in its `path`. That is one indexed query, not a recursive walk.

- [ ] **Step 1: Write the failing test**

```python
# append to backend/tests/services/test_export_service.py
import pytest
from beanie import PydanticObjectId

from app.models import Project
from app.services import export_service


@pytest.mark.asyncio
async def test_collect_includes_descendant_projects(clean_db):
    owner = "test-owner"
    parent = await Project(owner=owner, name="Parent", slug="parent").insert()
    child = await Project(
        owner=owner, name="Child", slug="child",
        parent_id=parent.id, path=[parent.id],
    ).insert()

    bundle = await export_service.collect(parent.id, owner=owner)

    ids = {p.id for p in bundle.projects}
    assert ids == {parent.id, child.id}


@pytest.mark.asyncio
async def test_collect_excludes_unrelated_projects(clean_db):
    owner = "test-owner"
    target = await Project(owner=owner, name="Target", slug="target").insert()
    await Project(owner=owner, name="Other", slug="other").insert()

    bundle = await export_service.collect(target.id, owner=owner)

    assert {p.id for p in bundle.projects} == {target.id}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_export_service.py -v`
Expected: FAIL — `AttributeError: module 'app.services.export_service' has no attribute 'collect'`

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/services/export_service.py`:

```python
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

    Descendants come from `Project.path`, which holds the ancestor chain --
    so every descendant of P has P in its path. One indexed query rather
    than a recursive walk.

    Owner-scoped throughout: an export must never reach into another
    profile's partition, and the root lookup is what stands between a
    request and someone else's project.
    """
    root = await Project.find_one(Project.id == project_id, Project.owner == owner)
    if root is None:
        raise NotFoundError(f"Project {project_id} not found")

    descendants = await Project.find(
        Project.owner == owner, {"path": project_id}
    ).to_list()
    projects = [root, *descendants]
    project_ids = [p.id for p in projects]

    objects = await DataObject.find(
        DataObject.owner == owner, {"project_id": {"$in": project_ids}}
    ).to_list()
    runs = await PipelineRun.find({"project_id": {"$in": project_ids}}).to_list()
    run_ids = [r.id for r in runs]
    run_jobs = (
        await RunJob.find({"run_id": {"$in": run_ids}}).to_list() if run_ids else []
    )

    blob_ids = sorted({o.blob_id for o in objects if o.blob_id is not None})
    blobs = (
        await Blob.find({"_id": {"$in": blob_ids}}).to_list() if blob_ids else []
    )

    job_ids = [j.job_id for j in run_jobs if getattr(j, "job_id", None)]
    timings = (
        await JobRunTiming.find({"job_id": {"$in": job_ids}}).to_list()
        if job_ids
        else []
    )

    return ExportBundle(
        projects=projects,
        objects=objects,
        runs=runs,
        run_jobs=run_jobs,
        timings=timings,
        blobs=blobs,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/services/test_export_service.py -v`
Expected: PASS (4 passed)

If `RunJob` has no `job_id` field, read `backend/app/models/run.py:199` and use the field that links a `RunJob` to its `Job`; adjust the `timings` query to match. Do not leave the `getattr` guard in place if the field exists — name it directly.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/export_service.py backend/tests/services/test_export_service.py
git commit -m "feat(export): collect a project subtree and everything it references"
```

---

### Task 3: Redaction

This is the task the whole feature's guarantee rests on. Write the greps first.

**Files:**
- Modify: `backend/app/services/export_service.py`
- Test: `backend/tests/services/test_export_service.py`

**Interfaces:**
- Consumes: `ExportBundle` from Task 2
- Produces: `def redact(bundle: ExportBundle) -> tuple[dict[str, list[dict]], RedactionSummary]`, and `RedactionSummary` with fields `paths_relativized: int`, `machine_records_cleared: int`, `profile: str`

`SERIALIZED_COLLECTIONS` is the allowlist. Adding a collection to the export is an edit to that tuple and nothing else — which is what makes the exclusion-by-construction rule checkable.

- [ ] **Step 1: Write the failing test**

```python
# append to backend/tests/services/test_export_service.py
from app.models import Blob, BlobState, JobRunTiming
from app.models.timing import RunMachine


@pytest.mark.asyncio
async def test_redact_strips_external_path(clean_db):
    bundle = export_service.ExportBundle(
        blobs=[Blob(size=10, state=BlobState.PRESENT,
                    external_path="/Users/gio/secret-dir/reads.fastq")]
    )

    docs, summary = export_service.redact(bundle)

    assert docs["blobs"][0]["external_path"] is None
    assert summary.paths_relativized == 1


@pytest.mark.asyncio
async def test_redact_clears_machine_identity_but_keeps_durations(clean_db):
    timing = JobRunTiming(
        job_id="j1", job_type="align", duration_seconds=2400.0,
        machine=RunMachine(machine_id="gio-workstation.local"),
    )
    bundle = export_service.ExportBundle(timings=[timing])

    docs, summary = export_service.redact(bundle)

    assert docs["job_timings"][0]["machine"] == {}
    assert docs["job_timings"][0]["duration_seconds"] == 2400.0
    assert summary.machine_records_cleared == 1


def test_serialized_collections_excludes_secret_bearing_collections():
    """Exclusion by construction: the allowlist is the guarantee.

    Inverts #411 deliberately -- backup fails safe by including every
    collection, export fails safe by naming the ones it serializes.
    """
    forbidden = {"ai_providers", "app_settings", "nodes", "profiles"}
    assert forbidden.isdisjoint(set(export_service.SERIALIZED_COLLECTIONS))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_export_service.py -v`
Expected: FAIL — `AttributeError: module 'app.services.export_service' has no attribute 'redact'`

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/services/export_service.py`:

```python
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
    needs it. `external_path` and anything else absolute leaks a username
    and directory layout, and means nothing on the recipient's machine.
    """
    removed = 0
    for key in ("external_path", "source_path", "bioinfo_home"):
        if doc.get(key) is not None:
            doc[key] = None
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
        if d.get("machine"):
            d["machine"] = {}
            summary.machine_records_cleared += 1
        docs["job_timings"].append(d)
    for blob in bundle.blobs:
        d = blob.model_dump(mode="json", by_alias=True)
        summary.paths_relativized += _strip_paths(d)
        docs["blobs"].append(d)

    return docs, summary
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/services/test_export_service.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/export_service.py backend/tests/services/test_export_service.py
git commit -m "feat(export): redact secrets, filesystem paths, and machine identity

Names the collections it serializes rather than dumping and subtracting.
This inverts ops/backup.sh deliberately: backup fails safe by including
every collection, export fails safe by excluding all but an allowlist."
```

---

### Task 4: The blob manifest

**Files:**
- Modify: `backend/app/services/export_service.py`
- Test: `backend/tests/services/test_export_service.py`

**Interfaces:**
- Consumes: `ExportBundle` from Task 2, `DEFAULT_BLOB_THRESHOLD_BYTES` from Task 1
- Produces: `def build_manifest(bundle: ExportBundle, *, threshold_bytes: int) -> tuple[str, list[Blob]]` — returns the TSV text and the blobs whose bytes to pack

The manifest lists every blob including excluded ones. That distinction is the manifest's whole value.

- [ ] **Step 1: Write the failing test**

```python
# append to backend/tests/services/test_export_service.py
@pytest.mark.asyncio
async def test_manifest_lists_excluded_blobs_as_excluded(clean_db):
    small = Blob(size=100, state=BlobState.PRESENT,
                 rel_path="ab/small", content_sha256="a" * 64)
    large = Blob(size=10_000, state=BlobState.PRESENT,
                 rel_path="cd/large", content_sha256="b" * 64)
    bundle = export_service.ExportBundle(blobs=[small, large])

    tsv, included = export_service.build_manifest(bundle, threshold_bytes=1_000)

    rows = [line.split("\t") for line in tsv.strip().splitlines()[1:]]
    by_size = {r[1]: r for r in rows}
    assert by_size["100"][-1] == "included"
    assert by_size["10000"][-1] == "excluded"
    assert [b.id for b in included] == [small.id]


@pytest.mark.asyncio
async def test_manifest_has_a_header_row(clean_db):
    bundle = export_service.ExportBundle(blobs=[])
    tsv, included = export_service.build_manifest(bundle, threshold_bytes=1_000)
    assert tsv.splitlines()[0].split("\t") == [
        "blob_id", "size", "content_sha256", "state", "rel_path", "bytes",
    ]
    assert included == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_export_service.py -v`
Expected: FAIL — `AttributeError: module 'app.services.export_service' has no attribute 'build_manifest'`

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/services/export_service.py`:

```python
_MANIFEST_HEADER = (
    "blob_id",
    "size",
    "content_sha256",
    "state",
    "rel_path",
    "bytes",
)


def build_manifest(
    bundle: ExportBundle, *, threshold_bytes: int
) -> tuple[str, list[Blob]]:
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
                    blob.content_sha256 or "",
                    str(blob.state),
                    blob.rel_path or "",
                    "included" if pack else "excluded",
                )
            )
        )

    return "\n".join(lines) + "\n", included
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/services/test_export_service.py -v`
Expected: PASS (9 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/export_service.py backend/tests/services/test_export_service.py
git commit -m "feat(export): list every blob in the manifest, flagging excluded bytes"
```

---

### Task 5: The human-readable report

Reuses `provenance_report.render_markdown` rather than adding a second renderer, so its gap markers (`**version not recorded**`) carry into the archive.

**Files:**
- Modify: `backend/app/services/export_service.py`
- Test: `backend/tests/services/test_export_service.py`

**Interfaces:**
- Consumes: `provenance_walker.walk(object_id, *, owner) -> ProvenanceChain`, `provenance_report.render_markdown(chain) -> str`, `ExportBundle` from Task 2
- Produces: `async def render_report(bundle: ExportBundle, *, owner: str) -> str`

- [ ] **Step 1: Write the failing test**

```python
# append to backend/tests/services/test_export_service.py
@pytest.mark.asyncio
async def test_report_names_the_project_and_each_object(clean_db):
    owner = "test-owner"
    project = await Project(owner=owner, name="Ecoli assembly",
                            slug="ecoli", description="Nanopore run").insert()
    bundle = export_service.ExportBundle(projects=[project])

    report = await export_service.render_report(bundle, owner=owner)

    assert "Ecoli assembly" in report
    assert "Nanopore run" in report


@pytest.mark.asyncio
async def test_report_states_the_archive_is_not_importable(clean_db):
    owner = "test-owner"
    project = await Project(owner=owner, name="P", slug="p").insert()
    bundle = export_service.ExportBundle(projects=[project])

    report = await export_service.render_report(bundle, owner=owner)

    assert "cannot be imported" in report.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_export_service.py -v`
Expected: FAIL — `AttributeError: module 'app.services.export_service' has no attribute 'render_report'`

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/services/export_service.py`:

```python
async def render_report(bundle: ExportBundle, *, owner: str) -> str:
    """Render report.md: the analysis, readable without BioFlow.

    Per-object provenance comes from `provenance_report.render_markdown`,
    the same renderer the History tab uses, rather than a second one. That
    is deliberate: it carries the renderer's gap markers into the archive,
    so a reader scanning for "which aligner version" sees the question
    asked and unanswered rather than seeing nothing and assuming it did not
    matter.
    """
    from app.services import provenance_report, provenance_walker

    root = bundle.projects[0]
    lines = [
        f"# {root.name}",
        "",
        "_Exported from BioFlow. This archive documents an analysis; it "
        "cannot be imported into another BioFlow instance._",
        "",
    ]
    if root.description:
        lines += [root.description, ""]

    if len(bundle.projects) > 1:
        lines += ["## Sub-projects", ""]
        lines += [f"- {p.name}" for p in bundle.projects[1:]]
        lines.append("")

    lines += ["## Files", ""]
    for obj in bundle.objects:
        lines += [f"### {obj.name}", ""]
        chain = await provenance_walker.walk(obj.id, owner=owner)
        lines += [provenance_report.render_markdown(chain), ""]

    if bundle.runs:
        lines += ["## Run history", ""]
        for run in bundle.runs:
            lines.append(f"- {run.kind} — {run.status} ({run.created_at:%Y-%m-%d})")
        lines.append("")

    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/services/test_export_service.py -v`
Expected: PASS (11 passed)

If `PipelineRun` has no `kind` or `status` attribute under those names, read `backend/app/models/run.py:117-145` and use the real field names.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/export_service.py backend/tests/services/test_export_service.py
git commit -m "feat(export): render the analysis report through provenance_report

Reuses render_markdown rather than adding a second renderer, so its gap
markers carry into the archive -- a reader scanning for a tool version
sees the question asked and unanswered rather than assuming it did not
matter."
```

---

### Task 6: Pack the archive

**Files:**
- Modify: `backend/app/services/export_service.py`
- Test: `backend/tests/services/test_export_service.py`

**Interfaces:**
- Consumes: everything from Tasks 1-5, `app.storage.paths.blob_path(digest) -> Path`
- Produces: `async def export_project(project_id, *, owner, threshold_bytes=DEFAULT_BLOB_THRESHOLD_BYTES) -> ExportResult`, with `ExportResult` fields `path: Path`, `size_bytes: int`, `blob_count: int`, `included_blob_count: int`, `redaction: RedactionSummary`

- [ ] **Step 1: Write the failing test**

The redaction greps are the assertions that outlive us. They go in this task, over the real produced archive.

```python
# append to backend/tests/services/test_export_service.py
import json
import tarfile


@pytest.mark.asyncio
async def test_archive_contains_the_expected_members(clean_db):
    owner = "test-owner"
    project = await Project(owner=owner, name="P", slug="p").insert()

    result = await export_service.export_project(project.id, owner=owner)

    with tarfile.open(result.path) as tar:
        names = set(tar.getnames())
    assert {"manifest.json", "data-manifest.tsv", "report.md", "README.md"} <= names
    assert any(n.startswith("metadata/") for n in names)


@pytest.mark.asyncio
async def test_manifest_json_carries_the_version_envelope(clean_db):
    owner = "test-owner"
    project = await Project(owner=owner, name="P", slug="p").insert()

    result = await export_service.export_project(project.id, owner=owner)

    with tarfile.open(result.path) as tar:
        manifest = json.loads(tar.extractfile("manifest.json").read())
    assert manifest["bioflow_export_version"] == export_service.BIOFLOW_EXPORT_VERSION
    assert manifest["redaction_profile"] == "secrets+paths+machine"


@pytest.mark.asyncio
async def test_objectids_are_preserved_for_a_future_importer(clean_db):
    owner = "test-owner"
    project = await Project(owner=owner, name="P", slug="p").insert()

    result = await export_service.export_project(project.id, owner=owner)

    with tarfile.open(result.path) as tar:
        docs = json.loads(tar.extractfile("metadata/projects.json").read())
    assert docs[0]["_id"] == str(project.id)


@pytest.mark.asyncio
async def test_archive_contains_no_secrets_no_paths_no_machine_names(clean_db):
    """The assertion that outlives whoever wrote the exporter.

    Greps the whole produced archive. This is what keeps the redaction rule
    true after someone edits export_service.py a year from now.
    """
    owner = "test-owner"
    project = await Project(owner=owner, name="P", slug="p").insert()
    await Blob(size=10, state=BlobState.PRESENT,
               external_path="/Users/gio/private/reads.fastq").insert()

    result = await export_service.export_project(project.id, owner=owner)

    with tarfile.open(result.path) as tar:
        blob = b""
        for member in tar.getmembers():
            if member.isfile():
                blob += tar.extractfile(member).read()

    for forbidden in (b"/Users/gio", b"secret.key", b"fernet", b"ai_providers"):
        assert forbidden.lower() not in blob.lower(), f"{forbidden!r} leaked"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_export_service.py -v`
Expected: FAIL — `AttributeError: module 'app.services.export_service' has no attribute 'export_project'`

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/services/export_service.py`:

```python
import asyncio
import io
import json
import tarfile
from datetime import UTC, datetime
from pathlib import Path

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
            src = blob_path(blob.content_sha256) if blob.content_sha256 else None
            if src and src.exists():
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
    dest = settings.exports_dir / f"{bundle.projects[0].slug}-{stamp}.tar.gz"

    manifest_json = {
        "bioflow_export_version": BIOFLOW_EXPORT_VERSION,
        "exported_at": datetime.now(UTC).isoformat(),
        "project_id": str(project_id),
        "project_name": bundle.projects[0].name,
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
```

Add `from app.config import settings` to the module's imports if it is not already there.

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/services/test_export_service.py -v`
Expected: PASS (15 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/export_service.py backend/tests/services/test_export_service.py
git commit -m "feat(export): pack the project archive with a versioned envelope

Greps the produced archive for secrets, absolute paths, and machine names
as a test, so the redaction guarantee is checked rather than trusted."
```

---

### Task 7: Queue handler and launcher

**Files:**
- Modify: `backend/app/queue/pipeline_handlers.py`
- Modify: `backend/app/services/pipeline_service.py`
- Modify: `backend/app/pipelines/node_types.py` (`EXCLUDED_LAUNCHES`, ~line 958)
- Test: `backend/tests/services/test_export_service.py`

**Interfaces:**
- Consumes: `export_service.export_project` from Task 6, `@handler` and `HandlerMode` from `app.queue.registry`
- Produces: `async def launch_project_export(*, project_id, owner, threshold_bytes=..., job_class=JobClass.USER_BACKGROUND) -> Job`

**`HandlerMode.THREAD`**, not SUBPROCESS: `registry.py:35-36` documents THREAD as "sync, CPU/IO-bound; run via asyncio.to_thread" and SUBPROCESS as "spawns processes; killed by process group." This packs a tarball in-process.

- [ ] **Step 1: Write the failing test**

The `EXCLUDED_LAUNCHES` assertion is the #355 trap. Run the **whole** `TestExhaustiveness` class, not just the one test.

```python
# append to backend/tests/services/test_export_service.py
def test_export_launcher_is_excluded_from_node_types():
    """Export is a project-level operation, not a pipeline node.

    node_types.py asserts every launch_* is either a NodeTypeSpec or
    explicitly excluded. #355 added both for one launcher in separate
    commits, satisfying the test its issue named while silently failing
    test_no_launcher_is_both_used_and_excluded in the same class.
    """
    from app.pipelines.node_types import EXCLUDED_LAUNCHES, NODE_TYPES

    name = "pipeline_service.launch_project_export"
    assert name in EXCLUDED_LAUNCHES
    launchers = {spec.launch for spec in NODE_TYPES.values()}
    assert name not in launchers
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_export_service.py::test_export_launcher_is_excluded_from_node_types -v`
Expected: FAIL — `assert 'pipeline_service.launch_project_export' in EXCLUDED_LAUNCHES`

If `NodeTypeSpec` has no `.launch` attribute, read `backend/app/pipelines/node_types.py` for the field naming the launcher and use it.

- [ ] **Step 3: Write minimal implementation**

In `backend/app/pipelines/node_types.py`, inside `EXCLUDED_LAUNCHES`:

```python
        # Project-level export producing a shareable archive on disk, not an
        # object a downstream node could consume -- the same class as
        # launch_summary and launch_gc_tracks. It also takes a project rather
        # than an object, so it has no input port to wire.
        "pipeline_service.launch_project_export",
```

In `backend/app/services/pipeline_service.py`:

```python
async def launch_project_export(
    *,
    project_id: PydanticObjectId,
    owner: str,
    threshold_bytes: int | None = None,
    job_class: JobClass = JobClass.USER_BACKGROUND,
) -> Job:
    """Queue an export of a project and its descendants to one archive.

    Raises rather than returning None when the project does not exist or
    belongs to another profile: an export is an explicit user action, and
    silence would look like a broken button.
    """
    from app.queue import queue
    from app.services import export_service, project_service

    project = await project_service.get_project(project_id, owner=owner)

    return await queue.enqueue(
        "project_export",
        payload={
            "project_id": str(project.id),
            "owner": owner,
            "threshold_bytes": (
                threshold_bytes
                if threshold_bytes is not None
                else export_service.DEFAULT_BLOB_THRESHOLD_BYTES
            ),
        },
        job_class=job_class,
        project_id=project.id,
    )
```

In `backend/app/queue/pipeline_handlers.py`:

```python
@handler(
    "project_export",
    mode=HandlerMode.THREAD,
    job_class=JobClass.USER_BACKGROUND,
    resources=JobResources(cpu=1, mem_mb=512, io=IoClass.HEAVY),
)
def project_export(ctx: JobContext) -> dict:
    """Pack one project and its descendants into a shareable archive.

    THREAD rather than SUBPROCESS: this packs a tarball in-process and
    spawns nothing. It is IO-heavy, which is what keeps it off the event
    loop.

    Reports what redaction removed, so the user can check what left the
    machine rather than trusting that it did.
    """
    from beanie import PydanticObjectId

    from app.db.client import run_from_thread
    from app.services import export_service

    result = run_from_thread(
        export_service.export_project(
            PydanticObjectId(ctx.payload["project_id"]),
            owner=ctx.payload["owner"],
            threshold_bytes=ctx.payload["threshold_bytes"],
        )
    )

    return {
        "archive_path": str(result.path),
        "size_bytes": result.size_bytes,
        "blob_count": result.blob_count,
        "included_blob_count": result.included_blob_count,
        "redaction": {
            "profile": result.redaction.profile,
            "paths_relativized": result.redaction.paths_relativized,
            "machine_records_cleared": result.redaction.machine_records_cleared,
        },
    }
```

A THREAD handler has no event loop. `run_from_thread` schedules the coroutine onto the connect-time loop — `asyncio.run()` here would make Motor raise "attached to a different loop." See CLAUDE.md's note on thread handlers and `summary_handlers.py:_resolve_sync`.

- [ ] **Step 4: Run the full exhaustiveness class, not just the one test**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_node_types.py -v`
Expected: PASS — every test in `TestExhaustiveness`, including `test_no_launcher_is_both_used_and_excluded`

Then: `./backend/run-worktree-tests.sh tests/services/test_export_service.py -v`
Expected: PASS (16 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/queue/pipeline_handlers.py backend/app/services/pipeline_service.py backend/app/pipelines/node_types.py backend/tests/services/test_export_service.py
git commit -m "feat(export): run the export as a queued job

THREAD mode, not SUBPROCESS: it packs a tarball in-process. Registered in
EXCLUDED_LAUNCHES since an export produces an archive on disk rather than
an object a downstream node could consume."
```

---

### Task 8: API endpoints

**Files:**
- Create: `backend/app/api/v1/exports.py`
- Modify: `backend/app/api/v1/__init__.py` (register the router)
- Test: `backend/tests/api/test_exports_api.py`

**Interfaces:**
- Consumes: `pipeline_service.launch_project_export` from Task 7, `settings.exports_dir` from Task 1
- Produces: `POST /api/v1/projects/{project_id}/export`, `GET /api/v1/exports`, `GET /api/v1/exports/{name}/download`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/api/test_exports_api.py
import pytest


@pytest.mark.asyncio
async def test_create_export_returns_a_job(client, clean_db, a_project):
    resp = await client.post(f"/api/v1/projects/{a_project.id}/export")
    assert resp.status_code == 202
    assert "job_id" in resp.json()


@pytest.mark.asyncio
async def test_create_export_404s_for_a_missing_project(client, clean_db):
    resp = await client.post("/api/v1/projects/000000000000000000000000/export")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_download_refuses_a_traversal_name(client, clean_db):
    resp = await client.get("/api/v1/exports/..%2F..%2Fsecret.key/download")
    assert resp.status_code in (400, 404)
```

Read `backend/tests/api/` for the actual `client` and project fixture names and match them; if no `a_project` fixture exists, create the project inline as in Task 2's tests.

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/api/test_exports_api.py -v`
Expected: FAIL — 404 on the POST route (router not registered)

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/api/v1/exports.py`:

```python
"""Create, list, and download project export archives."""

import re

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from app.api.deps import resolve_owner
from app.config import settings
from app.services import pipeline_service

router = APIRouter(tags=["exports"])

# An export filename is "<slug>-<timestamp>.tar.gz" and nothing else. The
# download route joins this onto a directory, so anything else is a
# traversal attempt, not a typo.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,128}\.tar\.gz$")


@router.post("/projects/{project_id}/export", status_code=202)
async def create_export(
    project_id: PydanticObjectId,
    threshold_bytes: int | None = None,
    owner: str = Depends(resolve_owner),
) -> dict:
    job = await pipeline_service.launch_project_export(
        project_id=project_id, owner=owner, threshold_bytes=threshold_bytes
    )
    return {"job_id": str(job.id)}


@router.get("/exports")
async def list_exports(owner: str = Depends(resolve_owner)) -> list[dict]:
    if not settings.exports_dir.exists():
        return []
    return sorted(
        (
            {
                "name": p.name,
                "size_bytes": p.stat().st_size,
                "created_at": p.stat().st_mtime,
            }
            for p in settings.exports_dir.glob("*.tar.gz")
        ),
        key=lambda e: e["created_at"],
        reverse=True,
    )


@router.get("/exports/{name}/download")
async def download_export(
    name: str, owner: str = Depends(resolve_owner)
) -> FileResponse:
    if not _SAFE_NAME.match(name):
        raise HTTPException(status_code=400, detail="Invalid export name")
    path = settings.exports_dir / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Export not found")
    return FileResponse(path, media_type="application/gzip", filename=name)
```

Register the router in `backend/app/api/v1/__init__.py` following the pattern the other routers there use.

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/api/test_exports_api.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/v1/exports.py backend/app/api/v1/__init__.py backend/tests/api/test_exports_api.py
git commit -m "feat(api): add endpoints to create, list, and download project exports"
```

---

### Task 9: Frontend export action

**Files:**
- Modify: the project-level action surface in `frontend/src/` — find it with `rg -n "projects/\$\{.*\}/" frontend/src --type ts` and follow the pattern the neighbouring project actions use
- Create: an export dialog component alongside the existing project dialogs

**Interfaces:**
- Consumes: `POST /api/v1/projects/{project_id}/export`, `GET /api/v1/exports` from Task 8

There is no headless component-testing setup in this repo (no jsdom, zero `.test.tsx` files) and none is expected — per CLAUDE.md, manual verification in the browser is the actual test for UI work.

- [ ] **Step 1: Find the existing pattern**

Run: `rg -n "export|Export" frontend/src --type tsx -l | head` and read a neighbouring project-level action to match its structure, naming, and API-call idiom. Do not invent a new pattern.

- [ ] **Step 2: Add the export action and dialog**

The dialog shows, before enqueueing:
- the blob-inclusion threshold, editable, defaulting to 100 MB
- the projected archive size at that threshold
- a plain statement of what is removed: API keys, absolute paths, machine names

On submit it POSTs and closes; progress appears in run history like any other job.

- [ ] **Step 3: Rebuild and verify in the browser**

From this worktree, per CLAUDE.md — never plain `docker compose` here:

```bash
./ops/worktree-up.sh
```

Open http://localhost:5273, find a project, run an export, and confirm the dialog shows a size and the job appears in run history.

- [ ] **Step 4: Commit**

```bash
git add frontend/src
git commit -m "feat(ui): add a project export action with a size-preview dialog"
```

- [ ] **Step 5: Bring the stack down**

A stack you brought up for testing is yours to bring back down:

```bash
./ops/worktree-up.sh --down
```

---

### Task 10: Manual pass over real data, then ship

A fixture cannot catch a provenance shape only real data has.

- [ ] **Step 1: Run the full backend suite**

Run: `./backend/run-worktree-tests.sh tests/ -q`
Expected: no failures. Read the count, not just the exit code.

- [ ] **Step 2: Export a real project and read the report**

Bring up the worktree stack, export a project that has real provenance — several objects, at least one multi-step pipeline run — download the archive, and read `report.md` end to end. Confirm the provenance reads correctly and that gap markers appear where facts genuinely were not recorded.

- [ ] **Step 3: Grep the real archive by hand**

```bash
tar xzf <archive>.tar.gz -O | grep -iE "secret|fernet|/Users/|api[_-]?key" | head
```

Expected: no matches. The automated grep runs over a fixture; this runs over real data, which has shapes a fixture does not.

- [ ] **Step 4: Bring the stack down**

```bash
./ops/worktree-up.sh --down
```

- [ ] **Step 5: Update the spec's HandlerMode note**

The spec says `SUBPROCESS`; the implementation is `THREAD` for the reason given in Task 7. Correct the spec so it matches what shipped.

```bash
git add docs/superpowers/specs/2026-08-17-project-export-archive-design.md
git commit -m "docs(export): correct the handler mode to THREAD

SUBPROCESS is for handlers that spawn processes and are killed by process
group. The exporter packs a tarball in-process."
```

- [ ] **Step 6: Rebase, push, open the PR, and merge when green**

```bash
git fetch origin main
git rebase origin/main
```

```bash
git diff origin/main...HEAD --stat
```

Confirm the file list matches what this plan set out to touch and nothing looks reverted, then:

```bash
git push -u origin HEAD
```

```bash
gh pr create --base main --fill
```

Put `Closes #476` in the PR description so the issue closes on merge, and label the PR `type:feature` and `area:backend` — `.github/release.yml` categorizes release notes by label, not by the title's prefix, so an unlabelled PR lands under "Other changes".

Poll `gh pr checks <N>` until every check reports pass — not pending — then:

```bash
gh pr merge <N> --rebase --delete-branch
```

`--rebase`, not `--squash`: `CHANGELOG.md` is generated from commit subjects *and bodies*, which a squash concatenates into one blob.

---

## Self-Review

**Spec coverage.** Every spec section maps to a task: archive layout → Tasks 4-6; scope and descendants → Task 2; redaction's three tiers and the #411 inversion → Task 3, with the greps in Task 6; `provenance_report.py` reuse → Task 5; `EXCLUDED_LAUNCHES` and the #355 trap → Task 7; exports directory and retention → Task 1; API and UI → Tasks 8-9; the manual pass → Task 10.

**Two corrections to the spec, both found by reading the code:**

1. **`HandlerMode.THREAD`, not `SUBPROCESS`.** `registry.py:36` reserves SUBPROCESS for handlers that spawn processes and are killed by process group. Task 10 Step 5 corrects the spec.
2. **`run_from_thread`, not `asyncio.run`.** A THREAD handler has no event loop, and `asyncio.run` would make Motor raise "attached to a different loop" — CLAUDE.md documents this as having already broken `summarize_object` once with every unit test green. Task 7 uses `run_from_thread`.

**Deliberately left for the implementer to read from the code**, rather than guessed at here: `RunJob`'s link field to `Job` (Task 2), `PipelineRun`'s status/kind field names (Task 5), `NodeTypeSpec`'s launcher attribute (Task 7), the API test fixtures (Task 8), and the frontend project-action pattern (Task 9). Each is flagged inline at the point it matters, with the file to read.

**Type consistency.** `ExportBundle` (Task 2) flows unchanged through `redact` (3), `build_manifest` (4), `render_report` (5), and `export_project` (6). `RedactionSummary` is produced in Task 3 and consumed in Tasks 6-7. `BIOFLOW_EXPORT_VERSION` and `DEFAULT_BLOB_THRESHOLD_BYTES` are defined in Task 1 and used in 6-8.
