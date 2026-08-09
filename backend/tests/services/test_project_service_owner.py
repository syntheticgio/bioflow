"""Owner scoping in project_service.

Per docs/superpowers/specs/2026-07-31-profiles-design.md, "Testing" --
asserting a profile *can* see its own data passes whether or not the filter
was ever applied. These assert what the OTHER profile cannot see.
"""

import pytest

from app.errors import NotFoundError
from app.models import Project
from app.services import project_service

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


class TestProjectServiceOwnerScoping:
    async def test_create_project_stamps_the_given_owner(self):
        project = await project_service.create_project(name="owner-stamp", owner="owner-a")

        assert project.owner == "owner-a"

    async def test_list_projects_excludes_other_owners(self):
        # Owner ids unique to this test: the database is module-scoped and
        # shared, so a generic "owner-a" would also pick up projects other
        # tests in this file created under it.
        await project_service.create_project(name="alpha", owner="list-owner-a")
        await project_service.create_project(name="beta", owner="list-owner-b")

        owner_a_projects = await project_service.list_projects(owner="list-owner-a")

        assert [p.name for p in owner_a_projects] == ["alpha"]

    async def test_get_project_raises_not_found_for_wrong_owner(self):
        """A wrong-owner lookup is indistinguishable from a missing one --
        deliberately. get_project already raises NotFoundError rather than
        returning None, and preserving that contract keeps every existing
        caller working unchanged."""
        project = await project_service.create_project(name="cross-get", owner="owner-a")

        with pytest.raises(NotFoundError):
            await project_service.get_project(project.id, owner="owner-b")

    async def test_same_name_allowed_across_owners(self):
        """The uniq_sibling_name index already leads with owner (project.py:41)
        -- this proves create_project actually uses that scoping rather than
        the index silently never being exercised because owner was always
        'local' before this feature."""
        a = await project_service.create_project(name="shared-name", owner="owner-a")
        b = await project_service.create_project(name="shared-name", owner="owner-b")

        assert a.id != b.id

    async def test_create_refuses_a_parent_owned_by_another_profile(self):
        """Otherwise a nested create builds a tree straddling the partition,
        and the child's materialized `path` names ancestors its owner can
        never resolve."""
        parent = await project_service.create_project(name="foreign-parent", owner="owner-a")

        with pytest.raises(NotFoundError):
            await project_service.create_project(
                name="trespassing-child", owner="owner-b", parent_id=parent.id
            )

    async def test_breadcrumbs_omit_ancestors_owned_by_another_profile(self):
        """The ancestor query reads Project.path, which is a list of ids with no
        owner of its own -- so an unscoped $in would resolve names across the
        partition. Only reachable if ids leak, but the filter costs nothing."""
        parent = await project_service.create_project(name="crumb-parent", owner="owner-a")
        child = await project_service.create_project(
            name="crumb-child", owner="owner-a", parent_id=parent.id
        )

        trail = await project_service.breadcrumbs(child, owner="owner-b")

        assert [c["name"] for c in trail] == ["crumb-child"]

    async def test_collect_subtree_stops_at_another_owners_child(self):
        """collect_subtree drives the delete, so anything it returns is a
        document that gets destroyed -- an unscoped descendant would be another
        profile's project deleted without appearing in its owner's preview.

        The stray child is inserted directly rather than through
        create_project, which now refuses to nest across owners. That refusal
        is the primary defence; this asserts the second one still holds if a
        cross-owner parent link ever arrives another way (a restore, a repair
        script, a future move-project).
        """
        parent = await project_service.create_project(name="subtree-parent", owner="owner-a")
        stray = Project(
            name="subtree-child",
            owner="owner-b",
            slug="subtree-child",
            parent_id=parent.id,
            path=[parent.id],
        )
        await stray.insert()

        found = await project_service.collect_subtree(parent.id, owner="owner-a")

        assert found == [parent.id]

    async def test_deletion_preview_raises_not_found_for_wrong_owner(self):
        project = await project_service.create_project(name="preview-scope", owner="owner-a")

        with pytest.raises(NotFoundError):
            await project_service.deletion_preview(project.id, owner="owner-b")

    async def test_delete_project_tree_destroys_nothing_for_wrong_owner(self):
        """The ownership check must run before any delete, not after: a
        wrong-owner call that discovers its mistake halfway has already
        destroyed another profile's rows."""
        project = await project_service.create_project(name="delete-scope", owner="owner-a")

        with pytest.raises(NotFoundError):
            await project_service.delete_project_tree(project.id, owner="owner-b")

        assert await project_service.get_project(project.id, owner="owner-a") is not None

    async def test_delete_project_raises_not_found_for_wrong_owner(self):
        project = await project_service.create_project(name="delete-one-scope", owner="owner-a")

        with pytest.raises(NotFoundError):
            await project_service.delete_project(project.id, owner="owner-b")

        assert await project_service.get_project(project.id, owner="owner-a") is not None

    async def test_update_project_raises_not_found_for_wrong_owner(self):
        project = await project_service.create_project(name="update-scope", owner="owner-a")

        with pytest.raises(NotFoundError):
            await project_service.update_project(
                project.id, {"name": "renamed-by-intruder"}, owner="owner-b"
            )

        unchanged = await project_service.get_project(project.id, owner="owner-a")
        assert unchanged.name == "update-scope"


class TestAgentSystemPrompt:
    async def test_defaults_to_empty_string(self):
        project = await project_service.create_project(
            name="prompt-default", owner="prompt-default-owner"
        )
        assert project.agent_system_prompt == ""

    async def test_update_sets_the_prompt(self):
        owner = "prompt-set-owner"
        project = await project_service.create_project(name="prompt-set", owner=owner)

        updated = await project_service.update_project(
            project.id, {"agent_system_prompt": "Always cite the tool."}, owner=owner
        )

        assert updated.agent_system_prompt == "Always cite the tool."

    async def test_empty_string_clears_the_prompt(self):
        """Reset-to-default sends "", which the None-skipping loop must honour."""
        owner = "prompt-clear-owner"
        project = await project_service.create_project(name="prompt-clear", owner=owner)
        await project_service.update_project(
            project.id, {"agent_system_prompt": "temporary"}, owner=owner
        )

        cleared = await project_service.update_project(
            project.id, {"agent_system_prompt": ""}, owner=owner
        )

        assert cleared.agent_system_prompt == ""

    async def test_none_leaves_the_prompt_alone(self):
        """A PATCH that omits the field must not wipe it."""
        owner = "prompt-untouched-owner"
        project = await project_service.create_project(
            name="prompt-untouched", owner=owner
        )
        await project_service.update_project(
            project.id, {"agent_system_prompt": "keep me"}, owner=owner
        )

        same = await project_service.update_project(
            project.id, {"name": "prompt-renamed"}, owner=owner
        )

        assert same.agent_system_prompt == "keep me"
