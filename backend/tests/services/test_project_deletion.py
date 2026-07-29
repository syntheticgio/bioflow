"""Project deletion: subtree collection, preview, and the cascade itself.

These tests need a real database. Refcount arithmetic and cascade ordering are
persistence behavior -- a fake would assert only that the fake was written
correctly.
"""

import pytest

from app.models import Project
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
