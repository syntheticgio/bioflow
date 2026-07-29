# Project Deletion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete a project and everything belonging to it — sub-projects, files, sidecar indexes, pipeline runs and jobs, and upload staging directories — from a button on the project information panel, plus a File-menu item that triggers blob GC on demand.

**Architecture:** Three new functions in `project_service` share one subtree traversal, so the deletion preview and the delete itself can never disagree about scope. The cascade delegates to `object_service.delete_object` rather than detaching blobs itself — that delegation is the fix for the sidecar-blob leak described in the spec. The frontend adds a `ProjectDangerZone` mirroring the existing file `DangerZone`, and a reusable `Menu` component replacing the inert header buttons.

**Tech Stack:** FastAPI + Beanie/Motor (MongoDB), pytest, React + TanStack Query, Vite.

**Spec:** `docs/superpowers/specs/2026-07-29-project-deletion-design.md`

---

## Before you start

**Read the spec first.** It explains *why* the current cascade leaks blobs; this plan assumes you understand that.

**Run everything in Docker.** Per `CLAUDE.md`, `docker compose` must be run from the **main repo root** (`/Users/syntheticgio/Programming/local-bio-pipeliner`), never from a worktree — the bind mounts are relative paths and running from a worktree silently repoints the shared stack at that branch.

Tests run inside the `api` container:

```bash
docker compose exec api python -m pytest tests/ -q
```

A bare host `.venv` hits Mongo replica-set errors the container's network does not have.

**`api` and `web` hot-reload; `worker` does not.** No task here changes a queue handler, so a worker restart is not required. If you find yourself editing anything under `backend/app/queue/`, stop — that is out of scope for this plan.

## One correction to the spec

The spec proposed a new `POST /jobs/maintenance/gc` route. **Do not build it.** `POST /schedules/{name}/run-now` already exists (`backend/app/api/v1/schedules.py:97`) and does exactly what is needed: it calls `scheduler.run_now`, which enqueues at `USER_INTERACTIVE` priority with a timestamped dedup key and returns the job id. Task 9 uses `POST /schedules/gc_blobs/run-now` instead. This removes an endpoint from the plan.

## File structure

**Backend — create:**
- `backend/tests/conftest.py` — shared `beanie_models` fixture (currently duplicated in two test files)
- `backend/tests/services/__init__.py`
- `backend/tests/services/test_project_deletion.py` — all deletion tests

**Backend — modify:**
- `backend/app/services/project_service.py` — add `collect_subtree`, `deletion_preview`, `delete_project_tree`; re-point `delete_project`
- `backend/app/api/v1/projects.py` — add the preview route
- `backend/app/api/v1/schemas.py` — add `DeletionPreviewOut`

**Frontend — create:**
- `frontend/src/components/Menu.tsx` — reusable dropdown
- `frontend/src/components/ProjectDangerZone.tsx` — the delete UI

**Frontend — modify:**
- `frontend/src/api/client.ts` — add `deletionPreview`, `runScheduleNow`
- `frontend/src/api/types.ts` — add `DeletionPreview`
- `frontend/src/components/DetailPanel.tsx` — render `ProjectDangerZone`
- `frontend/src/components/Header.tsx` — replace inert buttons with `Menu`

`ProjectDangerZone` is a separate file rather than another function inside `DetailPanel.tsx`: that file is already ~900 lines, and this is a self-contained unit with a narrow interface (`projectId`, `projectName`).

---

## Task 1: Shared Beanie test fixture

The deletion tests need a real database — refcounts and cascades are persistence behavior and cannot be faked. The `beanie_models` fixture already exists, copy-pasted, in `tests/storage/test_sidecars.py:18` and `tests/storage/test_object_role.py`. Move it to a shared conftest rather than making a third copy.

**Files:**
- Create: `backend/tests/conftest.py`

- [ ] **Step 1: Create the shared conftest**

```python
"""Shared test fixtures.

`beanie_models` is here rather than copy-pasted into each test module because
three files now need it. It targets a throwaway `biopipe_test` database, so it
never touches real data.
"""

import pytest
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from app.config import settings
from app.models import ALL_MODELS


@pytest.fixture(scope="module")
async def beanie_models():
    """Initialize Beanie against a throwaway database.

    Beanie refuses to instantiate a Document before init_beanie, even for an
    object that is never saved. Requested explicitly rather than autouse: it
    needs a running Mongo, and most tests in this suite are pure-function
    assertions that should not be dragged behind a database dependency.

    Collections are dropped on entry, not exit, so a failed run leaves its data
    behind for inspection.
    """
    from app.db.index_reconcile import reconcile_indexes

    client = AsyncIOMotorClient(settings.mongo_url, tz_aware=True)
    db = client["biopipe_test"]

    for model in ALL_MODELS:
        model_settings = model.Settings
        coll_name = getattr(model_settings, "name", model.__name__.lower())
        await db[coll_name].drop()
        indexes = getattr(model_settings, "indexes", [])
        if indexes:
            await reconcile_indexes(db[coll_name], indexes)

    await init_beanie(database=db, document_models=ALL_MODELS)
    yield
    client.close()
```

- [ ] **Step 2: Verify the existing suite still passes**

Run from the main repo root:

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: PASS, same count as before. The new conftest only *adds* a fixture; the two modules with their own copies keep using theirs (module-scoped fixtures shadow conftest ones of the same name), so nothing changes yet.

- [ ] **Step 3: Commit**

```bash
git add backend/tests/conftest.py
git commit -m "test: add shared beanie_models fixture"
```

---

## Task 2: `collect_subtree`

The single traversal both preview and delete build on.

**Files:**
- Modify: `backend/app/services/project_service.py`
- Create: `backend/tests/services/__init__.py`, `backend/tests/services/test_project_deletion.py`

- [ ] **Step 1: Create the test package marker**

Create `backend/tests/services/__init__.py` as an empty file.

- [ ] **Step 2: Write the failing test**

Create `backend/tests/services/test_project_deletion.py`:

```python
"""Project deletion: subtree collection, preview, and the cascade itself.

These tests need a real database. Refcount arithmetic and cascade ordering are
persistence behavior -- a fake would assert only that the fake was written
correctly.
"""

import pytest

from app.models import Project
from app.services import project_service

pytestmark = pytest.mark.usefixtures("beanie_models")


async def make_project(name: str, parent: Project | None = None) -> Project:
    return await project_service.create_project(
        name=name, parent_id=parent.id if parent else None
    )


class TestCollectSubtree:
    async def test_returns_just_the_root_when_there_are_no_children(self):
        root = await make_project("solo-subtree-root")
        assert await project_service.collect_subtree(root.id) == [root.id]

    async def test_includes_descendants_at_every_depth(self):
        """Three levels, because a two-level test passes even against an
        implementation that only looks at direct children."""
        root = await make_project("deep-root")
        child = await make_project("deep-child", root)
        grandchild = await make_project("deep-grandchild", child)

        found = await project_service.collect_subtree(root.id)

        assert found[0] == root.id
        assert set(found) == {root.id, child.id, grandchild.id}

    async def test_excludes_siblings_of_the_root(self):
        parent = await make_project("sibling-parent")
        target = await make_project("sibling-target", parent)
        other = await make_project("sibling-other", parent)

        found = await project_service.collect_subtree(target.id)

        assert other.id not in found
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
docker compose exec api python -m pytest tests/services/test_project_deletion.py -q
```

Expected: FAIL with `AttributeError: module 'app.services.project_service' has no attribute 'collect_subtree'`

- [ ] **Step 4: Implement `collect_subtree`**

Add to `backend/app/services/project_service.py`, after `delete_project`:

```python
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
```

A level-at-a-time walk rather than recursion: one query per depth instead of one per project.

- [ ] **Step 5: Run the test to verify it passes**

```bash
docker compose exec api python -m pytest tests/services/test_project_deletion.py -q
```

Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/project_service.py backend/tests/services/
git commit -m "feat: collect_subtree for project deletion scope"
```

---

## Task 3: `deletion_preview`

**Files:**
- Modify: `backend/app/services/project_service.py`
- Modify: `backend/tests/services/test_project_deletion.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/services/test_project_deletion.py`:

```python
class TestDeletionPreview:
    async def test_counts_objects_across_the_whole_subtree(self):
        """The denormalized project counters only cover one project, which is
        exactly why the preview exists -- nested contents must be counted."""
        from tests.services.helpers import make_object

        root = await make_project("preview-root")
        child = await make_project("preview-child", root)
        await make_object(root, "a.fastq.gz", size=100)
        await make_object(child, "b.fastq.gz", size=250)

        preview = await project_service.deletion_preview(root.id)

        assert preview["object_count"] == 2
        assert preview["total_bytes"] == 350
        assert preview["child_project_count"] == 1

    async def test_is_not_blocked_when_nothing_is_active(self):
        root = await make_project("preview-idle")
        preview = await project_service.deletion_preview(root.id)
        assert preview["blocked"] is False
        assert preview["active_jobs"] == []

    async def test_is_blocked_by_a_running_job(self):
        from tests.services.helpers import make_job

        root = await make_project("preview-running")
        await make_job(root, "align_bwa", "running")

        preview = await project_service.deletion_preview(root.id)

        assert preview["blocked"] is True
        assert preview["active_jobs"][0]["job_type"] == "align_bwa"
        assert preview["active_jobs"][0]["state"] == "running"

    async def test_is_blocked_by_a_delayed_job(self):
        """A DELAYED job awaiting backoff has not started but will. Deleting
        out from under it causes the exact mid-write race the block exists to
        prevent, so ACTIVE_STATES is the right predicate, not "running"."""
        from tests.services.helpers import make_job

        root = await make_project("preview-delayed")
        await make_job(root, "index_bam", "delayed")

        assert (await project_service.deletion_preview(root.id))["blocked"] is True

    async def test_is_not_blocked_by_a_finished_job(self):
        from tests.services.helpers import make_job

        root = await make_project("preview-finished")
        await make_job(root, "align_bwa", "succeeded")

        assert (await project_service.deletion_preview(root.id))["blocked"] is False

    async def test_is_blocked_by_a_job_in_a_descendant(self):
        from tests.services.helpers import make_job

        root = await make_project("preview-desc-root")
        child = await make_project("preview-desc-child", root)
        await make_job(child, "align_bwa", "queued")

        assert (await project_service.deletion_preview(root.id))["blocked"] is True
```

- [ ] **Step 2: Write the test helpers**

Create `backend/tests/services/helpers.py`:

```python
"""Factories for deletion tests.

Objects are created directly rather than through object_service.register_*,
which would require real bytes on disk and a running CAS. Deletion only cares
about the document graph and the blob refcount, both of which are set here
exactly as the ingest path sets them.
"""

from datetime import UTC, datetime

from beanie import PydanticObjectId

from app.models import (
    Blob,
    BlobStorage,
    DataObject,
    Job,
    JobState,
    ObjectStatus,
    Project,
    SidecarRole,
)


async def make_blob(digest: str, *, ref_count: int = 1) -> Blob:
    blob = Blob(
        id=digest,
        size=100,
        ref_count=ref_count,
        storage=BlobStorage.MANAGED,
        created_at=datetime.now(UTC),
    )
    await blob.insert()
    return blob


async def make_object(
    project: Project,
    name: str,
    *,
    size: int = 100,
    digest: str | None = None,
    sidecar_of: PydanticObjectId | None = None,
    sidecar_role: SidecarRole | None = None,
) -> DataObject:
    """An object plus the blob it references, with refcount 1.

    `digest` defaults to a unique per-object value; pass an explicit one to
    model two objects sharing content.
    """
    if digest is None:
        digest = f"{abs(hash(name)):064x}"[:64]
    if await Blob.get(digest) is None:
        await make_blob(digest)

    obj = DataObject(
        project_id=project.id,
        name=name,
        size=size,
        blob_sha256=digest,
        status=ObjectStatus.READY,
        sidecar_of=sidecar_of,
        sidecar_role=sidecar_role,
    )
    await obj.insert()
    return obj


async def make_job(project: Project, job_type: str, state: str) -> Job:
    job = Job(
        type=job_type,
        state=JobState(state),
        project_id=project.id,
    )
    await job.insert()
    return job
```

**Before implementing:** run

```bash
docker compose exec api python -c "from app.models import Blob, DataObject, Job; print([f for f in Blob.model_fields]); print([f for f in Job.model_fields])"
```

and reconcile the constructor calls above with the real required fields. Adjust `helpers.py` if a field is required that is not set here — do not adjust the tests.

- [ ] **Step 3: Run the tests to verify they fail**

```bash
docker compose exec api python -m pytest tests/services/test_project_deletion.py -q -k Preview
```

Expected: FAIL with `AttributeError: ... has no attribute 'deletion_preview'`

- [ ] **Step 4: Implement `deletion_preview`**

Add to `backend/app/services/project_service.py`:

```python
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
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
docker compose exec api python -m pytest tests/services/test_project_deletion.py -q
```

Expected: PASS (9 tests)

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/project_service.py backend/tests/services/
git commit -m "feat: deletion_preview with active-job blocking"
```

---

## Task 4: `delete_project_tree`

The core of the feature, including the sidecar-leak fix.

**Files:**
- Modify: `backend/app/services/project_service.py`
- Modify: `backend/tests/services/test_project_deletion.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/services/test_project_deletion.py`:

```python
class TestDeleteProjectTree:
    async def test_releases_a_sidecars_blob(self):
        """The regression that motivated this feature.

        The old cascade called detach_blob_from_object directly, skipping the
        sidecar cascade in delete_object. A BAM's .bai survived its parent with
        refcount 1 -- unreachable, and permanently un-GC-able because
        gc_candidates selects on ref_count <= 0.
        """
        from app.models import Blob
        from tests.services.helpers import make_object

        root = await make_project("sidecar-leak")
        bam = await make_object(root, "sample.bam", digest="a" * 64)
        await make_object(
            root,
            "sample.bam.bai",
            digest="b" * 64,
            sidecar_of=bam.id,
            sidecar_role=SidecarRole.BAI,
        )

        await project_service.delete_project_tree(root.id)

        assert (await Blob.get("b" * 64)).ref_count == 0
        assert await DataObject.find({"project_id": root.id}).count() == 0

    async def test_removes_every_project_in_the_subtree(self):
        root = await make_project("tree-root")
        child = await make_project("tree-child", root)
        grandchild = await make_project("tree-grandchild", child)

        await project_service.delete_project_tree(root.id)

        for pid in (root.id, child.id, grandchild.id):
            assert await Project.get(pid) is None

    async def test_removes_objects_in_descendants(self):
        from tests.services.helpers import make_object

        root = await make_project("tree-obj-root")
        child = await make_project("tree-obj-child", root)
        await make_object(child, "nested.fastq.gz")

        await project_service.delete_project_tree(root.id)

        assert await DataObject.find({"project_id": child.id}).count() == 0

    async def test_removes_runs_and_jobs(self):
        from app.models import Job, PipelineRun
        from tests.services.helpers import make_job

        root = await make_project("tree-jobs")
        await make_job(root, "align_bwa", "succeeded")

        await project_service.delete_project_tree(root.id)

        assert await Job.find({"project_id": root.id}).count() == 0
        assert await PipelineRun.find({"project_id": root.id}).count() == 0

    async def test_removes_upload_sessions_and_their_staging_dirs(self):
        """Staging directories are not refcounted and not shared, so unlike
        blobs they are removed synchronously rather than left to GC."""
        from pathlib import Path

        from app.models import UploadSession
        from app.services import upload_service

        root = await make_project("tree-uploads")
        # Returns (session, None) for a normal upload, or (None, object) when
        # the content was already held. No client digest is passed, so the
        # dedup short-circuit cannot fire and `session` is always set.
        session, _ = await upload_service.create_session(
            project_id=root.id, filename="pending.fastq.gz", total_size=1000
        )
        staging = Path(session.staging_dir)
        assert staging.exists()

        await project_service.delete_project_tree(root.id)

        assert not staging.exists()
        assert await UploadSession.find({"project_id": root.id}).count() == 0

    async def test_refuses_while_a_job_is_active(self):
        from app.errors import ConflictError
        from tests.services.helpers import make_job

        root = await make_project("tree-blocked")
        await make_job(root, "align_bwa", "running")

        with pytest.raises(ConflictError) as exc:
            await project_service.delete_project_tree(root.id)

        assert exc.value.details["active_jobs"][0]["job_type"] == "align_bwa"

    async def test_deletes_nothing_when_blocked(self):
        """A refusal must be total. A partial delete that then raises would
        leave the project half-destroyed with no way to tell."""
        from app.errors import ConflictError
        from tests.services.helpers import make_job, make_object

        root = await make_project("tree-blocked-intact")
        await make_object(root, "keep.fastq.gz")
        await make_job(root, "align_bwa", "running")

        with pytest.raises(ConflictError):
            await project_service.delete_project_tree(root.id)

        assert await Project.get(root.id) is not None
        assert await DataObject.find({"project_id": root.id}).count() == 1

    async def test_leaves_a_shared_blob_referenced(self):
        """Two objects, one blob. Deleting one project must decrement to 1,
        not to 0 -- the surviving file still needs those bytes."""
        from app.models import Blob
        from tests.services.helpers import make_object

        from app.db.client import get_db

        keep = await make_project("shared-keep")
        drop = await make_project("shared-drop")
        shared = "c" * 64
        await make_object(keep, "one.fastq.gz", digest=shared)
        await make_object(drop, "two.fastq.gz", digest=shared)
        # make_object only creates the blob once, so the second object needs
        # the increment the real attach path would have applied.
        await get_db().blobs.update_one({"_id": shared}, {"$inc": {"ref_count": 1}})

        await project_service.delete_project_tree(drop.id)

        assert (await Blob.get(shared)).ref_count == 1
```

Add `SidecarRole` and `DataObject` to the module's imports:

```python
from app.models import DataObject, Project, SidecarRole
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
docker compose exec api python -m pytest tests/services/test_project_deletion.py -q -k DeleteProjectTree
```

Expected: FAIL with `AttributeError: ... has no attribute 'delete_project_tree'`

- [ ] **Step 3: Implement `delete_project_tree`**

Add to `backend/app/services/project_service.py`:

```python
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

        for run in await PipelineRun.find(PipelineRun.project_id == pid).to_list():
            await RunJob.find(RunJob.run_id == run.id).delete()
            await run.delete()

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
```

Add the logger at the top of the module if it is not already there:

```python
import structlog

log = structlog.get_logger(__name__)
```

Check first — if `project_service.py` has no logger yet, add it below the imports.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
docker compose exec api python -m pytest tests/services/test_project_deletion.py -q
```

Expected: PASS (17 tests)

If `test_removes_upload_sessions_and_their_staging_dirs` errors inside
`require_home()` rather than failing an assertion, the test container has no
storage home configured. Guard that one test with a skip rather than weakening
the delete, adding this at the top of its body:

```python
from app.storage.home import check_home

if not check_home().ok:
    pytest.skip("needs a configured storage home")
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/project_service.py backend/tests/services/
git commit -m "feat: delete_project_tree, fixing the sidecar blob leak"
```

---

## Task 5: Re-point the existing cascade

`delete_project(cascade=True)` still runs the old leaking loop. Re-point it so the one existing caller gets the fixed behavior, rather than leaving a second, subtly-wrong delete path alive.

**Files:**
- Modify: `backend/app/services/project_service.py:112-133`
- Modify: `backend/tests/services/test_project_deletion.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/services/test_project_deletion.py`:

```python
class TestLegacyCascade:
    async def test_cascade_true_releases_sidecar_blobs(self):
        """The old cascade leaked here. Re-pointed at delete_project_tree so
        there is only one delete path to keep correct."""
        from app.models import Blob
        from tests.services.helpers import make_object

        root = await make_project("legacy-cascade")
        bam = await make_object(root, "legacy.bam", digest="d" * 64)
        await make_object(
            root,
            "legacy.bam.bai",
            digest="e" * 64,
            sidecar_of=bam.id,
            sidecar_role=SidecarRole.BAI,
        )

        await project_service.delete_project(root.id, cascade=True)

        assert (await Blob.get("e" * 64)).ref_count == 0

    async def test_cascade_false_still_refuses_a_non_empty_project(self):
        from app.errors import ConflictError
        from tests.services.helpers import make_object

        root = await make_project("legacy-refuse")
        await make_object(root, "blocker.fastq.gz")

        with pytest.raises(ConflictError):
            await project_service.delete_project(root.id, cascade=False)

        assert await Project.get(root.id) is not None

    async def test_cascade_false_still_deletes_an_empty_project(self):
        root = await make_project("legacy-empty")
        await project_service.delete_project(root.id, cascade=False)
        assert await Project.get(root.id) is None
```

- [ ] **Step 2: Run to verify the first test fails**

```bash
docker compose exec api python -m pytest tests/services/test_project_deletion.py -q -k Legacy
```

Expected: `test_cascade_true_releases_sidecar_blobs` FAILS (`ref_count == 1`, not 0) — reproducing the original bug through the old entry point. The other two PASS.

- [ ] **Step 3: Re-point the cascade**

Replace the body of `delete_project` in `backend/app/services/project_service.py` (currently lines 112-133) with:

```python
async def delete_project(project_id: PydanticObjectId, *, cascade: bool = False) -> None:
    """Delete a project, optionally with everything inside it.

    `cascade=True` delegates to delete_project_tree rather than detaching blobs
    itself. The hand-rolled loop this replaces skipped object_service's sidecar
    cascade and stranded index blobs at refcount 1, where GC could never reach
    them.
    """
    project = await get_project(project_id)

    object_count = await DataObject.find(DataObject.project_id == project_id).count()
    child_count = await Project.find(Project.parent_id == project_id).count()

    if not cascade and (object_count or child_count):
        raise ConflictError(
            "Project is not empty. Delete its contents first, or pass cascade=true.",
            details={"object_count": object_count, "child_project_count": child_count},
        )

    if cascade:
        await delete_project_tree(project_id)
        return

    await project.delete()
```

Note `delete_project_tree` must be defined *above* `delete_project`, or Python resolves the name at call time anyway (module-level function, so either order works — but keep the file readable by placing `collect_subtree`, `deletion_preview`, and `delete_project_tree` above it).

- [ ] **Step 4: Run to verify all pass**

```bash
docker compose exec api python -m pytest tests/services/test_project_deletion.py -q
```

Expected: PASS (20 tests)

- [ ] **Step 5: Run the whole suite**

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: PASS. If anything else called `delete_project(cascade=True)` and depended on the old behavior, it surfaces here.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/project_service.py backend/tests/services/
git commit -m "fix: route cascade delete through delete_project_tree"
```

---

## Task 6: Preview endpoint

**Files:**
- Modify: `backend/app/api/v1/schemas.py`
- Modify: `backend/app/api/v1/projects.py`

- [ ] **Step 1: Add the response schema**

Append to `backend/app/api/v1/schemas.py`:

```python
class ActiveJobOut(BaseModel):
    id: str
    job_type: str
    state: str


class DeletionPreviewOut(BaseModel):
    """What deleting a project would destroy, and whether it may proceed."""

    project_ids: list[str]
    child_project_count: int
    object_count: int
    total_bytes: int
    run_count: int
    job_count: int
    upload_session_count: int
    active_jobs: list[ActiveJobOut]
    blocked: bool
```

If `BaseModel` is not already imported in that file, add `from pydantic import BaseModel`.

- [ ] **Step 2: Add the route**

In `backend/app/api/v1/projects.py`, add immediately after `delete_project` (line 63-65):

```python
@router.get("/{project_id}/deletion-preview", response_model=DeletionPreviewOut)
async def project_deletion_preview(project_id: PydanticObjectId) -> DeletionPreviewOut:
    """Counts and blockers for a delete, so the confirmation can be specific."""
    return DeletionPreviewOut(**await project_service.deletion_preview(project_id))
```

Add `DeletionPreviewOut` to the existing import block from `app.api.v1.schemas`.

- [ ] **Step 3: Verify the route responds**

The `api` container hot-reloads, so no restart is needed. Create a project in the UI at localhost:5173, copy its id from the panel, then:

```bash
curl -s localhost:5173/api/v1/projects/PROJECT_ID/deletion-preview | python3 -m json.tool
```

Expected: a JSON object with `blocked: false`, `object_count` matching the project's contents, and `child_project_count: 0`.

- [ ] **Step 4: Commit**

```bash
git add backend/app/api/v1/schemas.py backend/app/api/v1/projects.py
git commit -m "feat: project deletion-preview endpoint"
```

---

## Task 7: Frontend API client

**Files:**
- Modify: `frontend/src/api/types.ts`
- Modify: `frontend/src/api/client.ts`

- [ ] **Step 1: Add the types**

Append to `frontend/src/api/types.ts`:

```typescript
export interface ActiveJob {
  id: string;
  job_type: string;
  state: string;
}

export interface DeletionPreview {
  project_ids: string[];
  child_project_count: number;
  object_count: number;
  total_bytes: number;
  run_count: number;
  job_count: number;
  upload_session_count: number;
  active_jobs: ActiveJob[];
  blocked: boolean;
}
```

- [ ] **Step 2: Add the client methods**

In `frontend/src/api/client.ts`, add directly after `deleteProject`:

```typescript
  deletionPreview: (id: string) =>
    request<DeletionPreview>(`/projects/${id}/deletion-preview`),

  runScheduleNow: (name: string) =>
    request<{ name: string; job_id: string }>(`/schedules/${name}/run-now`, {
      method: "POST",
    }),
```

Add `DeletionPreview` to the type import at the top of the file.

- [ ] **Step 3: Verify it compiles**

```bash
docker compose exec web npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/api/client.ts
git commit -m "feat: deletionPreview and runScheduleNow client methods"
```

---

## Task 8: `ProjectDangerZone`

**Files:**
- Create: `frontend/src/components/ProjectDangerZone.tsx`
- Modify: `frontend/src/components/DetailPanel.tsx`

- [ ] **Step 1: Create the component**

Create `frontend/src/components/ProjectDangerZone.tsx`:

```tsx
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { DeletionPreview } from "../api/types";
import { formatBytes } from "../lib/format";
import { notify } from "../stores/messageStore";

/** "3 sub-projects, 47 files (2.1 GB), and 12 pipeline runs".
 *
 *  Zero-valued clauses are dropped, so an empty project produces an empty
 *  string and the caller falls back to the bare "Delete X?" wording. */
function describeContents(p: DeletionPreview): string {
  const parts: string[] = [];
  if (p.child_project_count > 0) {
    parts.push(
      `${p.child_project_count} sub-project${p.child_project_count === 1 ? "" : "s"}`,
    );
  }
  if (p.object_count > 0) {
    parts.push(
      `${p.object_count} file${p.object_count === 1 ? "" : "s"} (${formatBytes(p.total_bytes)})`,
    );
  }
  if (p.run_count > 0) {
    parts.push(`${p.run_count} pipeline run${p.run_count === 1 ? "" : "s"}`);
  }
  if (parts.length === 0) return "";
  if (parts.length === 1) return parts[0];
  return `${parts.slice(0, -1).join(", ")}, and ${parts[parts.length - 1]}`;
}

export function ProjectDangerZone({
  projectId,
  projectName,
}: {
  projectId: string;
  projectName: string;
}) {
  const [confirming, setConfirming] = useState(false);
  const qc = useQueryClient();
  const navigate = useNavigate();

  // Only fetched once the user asks, so browsing a project costs no extra
  // request. Never cached: an active job that ends must not leave a stale
  // block in place.
  const preview = useQuery({
    queryKey: ["project", projectId, "deletion-preview"],
    queryFn: () => api.deletionPreview(projectId),
    enabled: confirming,
    gcTime: 0,
    staleTime: 0,
  });

  const remove = useMutation({
    mutationFn: () => api.deleteProject(projectId, true),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      qc.removeQueries({ queryKey: ["project", projectId] });
      notify.success(
        `Deleted ${projectName} — disk space is reclaimed by the next cleanup pass (File → Clean up storage now).`,
      );
      // The selected project no longer exists, so the panel must not keep
      // pointing at it.
      navigate("/");
    },
    onError: (e: Error) => {
      // A job may have started between preview and confirm. Re-fetching turns
      // the generic failure back into the specific "still active" message.
      preview.refetch();
      notify.error(e.message);
    },
  });

  if (!confirming) {
    return (
      <div className="section">
        <div className="section-title">Delete</div>
        <button
          type="button"
          className="btn danger"
          onClick={() => setConfirming(true)}
        >
          Delete project
        </button>
        <div style={{ color: "var(--text-faint)", fontSize: 11, marginTop: 6 }}>
          Removes this project, everything inside it, and its pipeline history.
        </div>
      </div>
    );
  }

  const cancel = (
    <button
      type="button"
      className="btn"
      onClick={() => setConfirming(false)}
      disabled={remove.isPending}
    >
      Cancel
    </button>
  );

  return (
    <div className="section">
      <div className="section-title">Delete</div>
      <div className="error-box" style={{ marginBottom: 0 }}>
        {preview.isLoading ? (
          <div>Checking what this would delete…</div>
        ) : preview.isError ? (
          <>
            <div style={{ marginBottom: 8 }}>
              Couldn't check this project's contents: {preview.error.message}
            </div>
            <div style={{ display: "flex", gap: 8 }}>{cancel}</div>
          </>
        ) : preview.data?.blocked ? (
          <>
            <div style={{ marginBottom: 8 }}>
              Can't delete yet — {preview.data.active_jobs.length} job
              {preview.data.active_jobs.length === 1 ? " is" : "s are"} still
              active in this project.
              <div style={{ fontSize: 11, marginTop: 4 }}>
                {preview.data.active_jobs
                  .map((j) => `${j.job_type} — ${j.state}`)
                  .join(", ")}
              </div>
              <div style={{ marginTop: 6 }}>
                Wait for them to finish, or cancel them, then try again.
              </div>
            </div>
            <div style={{ display: "flex", gap: 8 }}>{cancel}</div>
          </>
        ) : (
          <>
            <div style={{ marginBottom: 8 }}>
              Delete <strong>{projectName}</strong>
              {describeContents(preview.data!)
                ? `, including ${describeContents(preview.data!)}`
                : ""}
              ? This cannot be undone.
            </div>
            <div style={{ display: "flex", gap: 8 }}>
              <button
                type="button"
                className="btn danger"
                onClick={() => remove.mutate()}
                disabled={remove.isPending}
              >
                {remove.isPending ? "Deleting…" : "Yes, delete"}
              </button>
              {cancel}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
```

The active-job state is shown per job (`align_bwa — queued`) rather than saying everything is "running": `ACTIVE_STATES` includes pending, queued, and delayed jobs that have not started, and calling a queued job "running" sends the user looking for work that is not happening.

- [ ] **Step 2: Render it in the project panel**

In `frontend/src/components/DetailPanel.tsx`, inside `ProjectDetail`, add after the `<JobList projectId={project.id} />` block (around line 217):

```tsx
      <ProjectDangerZone projectId={project.id} projectName={project.name} />
```

Add the import at the top:

```tsx
import { ProjectDangerZone } from "./ProjectDangerZone";
```

- [ ] **Step 3: Verify it compiles**

```bash
docker compose exec web npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 4: Verify in the browser**

At localhost:5173 (`web` hot-reloads):

1. Select a project with files. The **Delete** section appears below the job list.
2. Click **Delete project** — the confirm text names the file count and size.
3. Click **Cancel** — it returns to the button.
4. Start a pipeline job in that project, click **Delete project** again — the blocked message names the job and its state, with no confirm button.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/ProjectDangerZone.tsx frontend/src/components/DetailPanel.tsx
git commit -m "feat: project delete button with blast-radius confirmation"
```

---

## Task 9: Header menu and manual GC

**Files:**
- Create: `frontend/src/components/Menu.tsx`
- Modify: `frontend/src/components/Header.tsx`

- [ ] **Step 1: Create the reusable menu**

Create `frontend/src/components/Menu.tsx`:

```tsx
import { useEffect, useRef, useState } from "react";

export interface MenuItem {
  label: string;
  onSelect: () => void;
  disabled?: boolean;
}

/** A header dropdown: click to open, Escape or outside-click to close,
 *  arrow keys to move between items.
 *
 *  General rather than File-specific because that behavior *is* the component
 *  — View and Help can adopt it without rework. */
export function Menu({ label, items }: { label: string; items: MenuItem[] }) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!root.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const choose = (item: MenuItem) => {
    if (item.disabled) return;
    setOpen(false);
    item.onSelect();
  };

  return (
    <div ref={root} style={{ position: "relative", display: "inline-block" }}>
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => {
          setOpen((v) => !v);
          setActive(0);
        }}
      >
        {label}
      </button>

      {open && (
        <div
          role="menu"
          className="menu-dropdown"
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") {
              e.preventDefault();
              setActive((i) => (i + 1) % items.length);
            } else if (e.key === "ArrowUp") {
              e.preventDefault();
              setActive((i) => (i - 1 + items.length) % items.length);
            } else if (e.key === "Enter" && items[active]) {
              e.preventDefault();
              choose(items[active]);
            }
          }}
        >
          {items.length === 0 ? (
            <div className="menu-empty">Nothing here yet</div>
          ) : (
            items.map((item, i) => (
              <button
                key={item.label}
                type="button"
                role="menuitem"
                className={i === active ? "menu-item active" : "menu-item"}
                disabled={item.disabled}
                autoFocus={i === 0}
                onMouseEnter={() => setActive(i)}
                onClick={() => choose(item)}
              >
                {item.label}
              </button>
            ))
          )}
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Add the styles**

Append to `frontend/src/styles.css`:

```css
.menu-dropdown {
  position: absolute;
  top: 100%;
  left: 0;
  z-index: 50;
  min-width: 190px;
  padding: 4px;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 4px;
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.3);
}

.menu-item {
  display: block;
  width: 100%;
  padding: 6px 10px;
  text-align: left;
  background: none;
  border: none;
  border-radius: 3px;
  color: var(--text);
  cursor: pointer;
}

.menu-item.active:not(:disabled) {
  background: var(--bg-hover);
}

.menu-item:disabled {
  color: var(--text-faint);
  cursor: default;
}

.menu-empty {
  padding: 6px 10px;
  color: var(--text-faint);
  font-size: 11px;
}
```

Check the variable names against the top of `styles.css` first — if `--bg-panel` or `--bg-hover` do not exist, substitute the nearest equivalents already in use.

- [ ] **Step 3: Wire it into the header**

In `frontend/src/components/Header.tsx`, replace the `MENUS` constant and the `{MENUS.map(...)}` block.

Delete:

```tsx
/** Placeholders still awaiting real actions. */
const MENUS = ["File", "View", "Help"];
```

Replace the map block with:

```tsx
        <Menu
          label="File"
          items={[
            {
              label: "Clean up storage now",
              onSelect: () => cleanUp.mutate(),
              disabled: cleanUp.isPending,
            },
          ]}
        />
        <Menu label="View" items={[]} />
        <Menu label="Help" items={[]} />
```

Add inside the `Header` function, above the `return`:

```tsx
  const cleanUp = useMutation({
    mutationFn: () => api.runScheduleNow("gc_blobs"),
    onSuccess: () => {
      // The job is queued, not finished -- gc_blobs runs on the worker, so its
      // reclaim counts are not available here. Point at Activity instead of
      // inventing a number.
      notify.success("Storage cleanup started. Progress is in Activity.");
    },
    onError: (e: Error) => notify.error(e.message),
  });
```

Add the imports:

```tsx
import { useMutation, useQuery } from "@tanstack/react-query";
import { notify } from "../stores/messageStore";
import { Menu } from "./Menu";
```

(`useQuery` is already imported; extend that line rather than duplicating it.)

**Note a deviation from the spec here.** The spec said the menu item would toast the reclaim counts ("Reclaimed 12 files"). It cannot: `run_now` enqueues the job and returns a job id immediately, while `gc_blobs` executes later on the worker. Reporting counts would require polling the job to completion. Starting-confirmation plus a pointer to Activity is the honest version; if per-run counts are wanted in the UI later, that is its own piece of work.

- [ ] **Step 4: Verify it compiles**

```bash
docker compose exec web npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 5: Verify in the browser**

At localhost:5173:

1. Click **File** — the dropdown opens with "Clean up storage now".
2. Press Escape — it closes. Click **File** again, click outside — it closes.
3. Press the down arrow — the item highlights.
4. Click **Clean up storage now** — the toast confirms the cleanup started.
5. Open **Activity** — a `gc_blobs` job appears.
6. Click **View** and **Help** — each opens showing "Nothing here yet".

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/Menu.tsx frontend/src/components/Header.tsx frontend/src/styles.css
git commit -m "feat: header menu with manual storage cleanup"
```

---

## Task 10: End-to-end verification

No code. This is the spec's verification section, run against the real app.

- [ ] **Step 1: Confirm the stack is serving main, not a worktree**

```bash
docker inspect biopipe-worker-1 --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}'
```

If any path contains `.claude/worktrees/`, the stack is on the wrong tree. Fix by rebuilding from the main repo root before continuing.

- [ ] **Step 2: Build the scenario**

At localhost:5173: create a project, create a sub-project inside it, upload a file to each, and align one to produce a BAM with a `.bai` sidecar.

- [ ] **Step 3: Confirm the preview counts are right**

Click **Delete project** on the parent. The confirm text should name the sub-project, the total file count (including the BAM and its `.bai`), and the combined size. Cancel.

- [ ] **Step 4: Confirm the block works**

Start an alignment, immediately click **Delete project**. Expected: the blocked message names the job and its state, with no confirm button offered.

- [ ] **Step 5: Delete and confirm removal**

Let the job finish. Delete. Expected: both projects leave the explorer, the panel clears, and the toast names the project.

- [ ] **Step 6: Confirm the sidecar blob is released — the core fix**

```bash
docker compose exec api python -c "
import asyncio
from app.db.client import get_db, connect
async def main():
    await connect()
    n = await get_db().blobs.count_documents({'ref_count': {'\$lte': 0}})
    print('blobs at refcount 0:', n)
asyncio.run(main())
"
```

Expected: a non-zero count including the `.bai`'s blob. Before this change that blob would have been stranded at 1 forever.

Adjust the connect/bootstrap call to match whatever `app/db/client.py` actually exposes.

- [ ] **Step 7: Reclaim the bytes**

Note the size of the object store, run **File → Clean up storage now**, wait for the job to finish in Activity, then check again:

```bash
docker compose exec api du -sh /data/objects
```

Expected: smaller. Adjust the path if `settings.objects_dir` differs.

Blobs are only unlinked after their refcount has been zero past the grace window (`blob_service.py:20`), so a cleanup run immediately after the delete may reclaim nothing. That is correct behavior, not a failure — wait out the window and run it again.

- [ ] **Step 8: Confirm staging directories are gone**

```bash
docker compose exec api ls /data/staging
```

Expected: no directories belonging to the deleted projects' upload sessions.

- [ ] **Step 9: Full suite, one last time**

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: PASS.

- [ ] **Step 10: Commit any fixes**

If steps 1-9 surfaced problems, fix and commit them. If everything passed, there is nothing to commit and the feature is done.
