"""Project deletion: subtree collection, preview, and the cascade itself.

These tests need a real database. Refcount arithmetic and cascade ordering are
persistence behavior -- a fake would assert only that the fake was written
correctly.
"""

import pytest

from app.models import DataObject, Project, SidecarRole
from app.services import project_service

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


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
        from app.storage.home import check_home

        if not check_home().ok:
            pytest.skip("needs a configured storage home")

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
        from app.db.client import get_db
        from app.models import Blob
        from tests.services.helpers import make_object

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
