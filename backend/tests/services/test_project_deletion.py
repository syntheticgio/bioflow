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
