"""Owner scoping in object_service.

Per docs/superpowers/specs/2026-07-31-profiles-design.md, "Testing" -- asserting
a profile *can* see its own data passes whether or not the filter was ever
applied, so these assert what the OTHER profile cannot see.

Everything here drives the real ingest path rather than tests/services/helpers.py's
factories. Those factories set `owner=project.owner` by hand, which is precisely
the owner production did not set: a test built on them would go green while the
writer half of this feature was still missing entirely.
"""

import uuid
from pathlib import Path

import pytest

from app.config import settings
from app.errors import NotFoundError
from app.models import Blob, DataObject
from app.services import object_service, project_service

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest.fixture(autouse=True)
def _no_queue(monkeypatch):
    """Stub the header-parse enqueue.

    `ingest_local_file` finishes by queueing `ingest_headers`, which needs a
    live Redis connection this process never opens. That enqueue is orthogonal
    to the owner boundary under test -- carrying `owner` into the job document
    is Task 8's job, and stubbing here keeps a Redis outage from being reported
    as an isolation failure.
    """

    async def _skip(obj, **kwargs):
        return ""

    monkeypatch.setattr(object_service, "enqueue_ingest", _skip)


def _scratch_file(content: bytes) -> Path:
    """A file for ingest to consume, under tmp_dir.

    Sync on purpose: blocking file I/O inside an async def is what ASYNC240
    objects to, and there is nothing to overlap with here.

    `ingest_local_file` consumes its argument -- it renames the file into the
    object store -- and tmp_dir shares a filesystem with objects/, which makes
    that placement an atomic rename rather than a copy.
    """
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    path = settings.tmp_dir / f"owner-test-{uuid.uuid4().hex}.txt"
    path.write_bytes(content)
    return path


async def _ingest(project, *, owner: str, content: bytes) -> DataObject:
    """Ingest real bytes through the production path.

    Content is unique per call because a byte-identical file deduplicates onto
    an existing blob, which would make a refcount assertion read someone else's
    number.
    """
    path = _scratch_file(content)
    return await object_service.ingest_local_file(
        project_id=project.id,
        path=path,
        name=path.name,
        owner=owner,
    )


class TestObjectServiceOwnerScoping:
    async def test_ingest_local_file_stamps_the_given_owner(self):
        """The writer half. Without it every object inherits the "local"
        default from TimestampedDocument and the owner-scoped deletion cascade
        below matches nothing."""
        project = await project_service.create_project(
            name="ingest-stamp", owner="obj-stamp-a"
        )

        obj = await _ingest(project, owner="obj-stamp-a", content=uuid.uuid4().bytes)

        assert obj.owner == "obj-stamp-a"

    async def test_list_objects_excludes_other_owners(self):
        # Owner ids unique to this test: the database is module-scoped and
        # shared, so a generic "owner-a" would also pick up other tests' rows.
        mine_project = await project_service.create_project(
            name="list-scope", owner="obj-list-a"
        )
        # Each owner ingests into its own project, because ingest now refuses a
        # project belonging to another profile. The listing is still queried by
        # project id, so this asserts the owner clause rather than the
        # project_id one only if a stray same-project row can exist -- hence
        # the direct insert below, which models a row this filter must exclude
        # however it got there (a restore, a repair script, a pre-partition
        # document carrying the old "local" default).
        theirs_project = await project_service.create_project(
            name="list-scope-other", owner="obj-list-b"
        )
        mine = await _ingest(mine_project, owner="obj-list-a", content=uuid.uuid4().bytes)
        await _ingest(theirs_project, owner="obj-list-b", content=uuid.uuid4().bytes)
        stray = DataObject(
            project_id=mine_project.id, owner="obj-list-b", name="stray.txt"
        )
        await stray.insert()

        listed = await object_service.list_objects(mine_project.id, owner="obj-list-a")

        assert [o.id for o in listed] == [mine.id]

    async def test_get_object_raises_not_found_for_wrong_owner(self):
        """A wrong-owner lookup is indistinguishable from a missing one, and
        get_object already raises rather than returning None -- preserving that
        keeps every existing caller's error handling working unchanged."""
        project = await project_service.create_project(
            name="get-scope", owner="obj-get-a"
        )
        obj = await _ingest(project, owner="obj-get-a", content=uuid.uuid4().bytes)

        with pytest.raises(NotFoundError):
            await object_service.get_object(obj.id, owner="obj-get-b")

    async def test_ingest_refuses_a_project_owned_by_another_profile(self):
        """Otherwise one profile writes objects into another's project, and the
        owner filter on the cascade then strands them there."""
        project = await project_service.create_project(
            name="cross-ingest", owner="obj-cross-a"
        )

        with pytest.raises(NotFoundError):
            await _ingest(project, owner="obj-cross-b", content=uuid.uuid4().bytes)

    async def test_delete_project_tree_reclaims_a_non_local_owners_objects(self):
        """The leak this task closes, end to end.

        delete_project_tree filters DataObject by owner. Before ingest set that
        field, a non-"local" project's cascade matched nothing: the project
        document was deleted while its objects survived as orphans pointing at a
        dead project_id, their blobs never decremented -- stranded at refcount 1
        where GC can never reach them -- and the log line reported objects=0
        while doing it.

        Driven through ingest_local_file on purpose. The factories in
        tests/services/helpers.py set the owner by hand, so this passes there
        whether or not the production writer was ever fixed.
        """
        owner = "obj-cascade-a"
        project = await project_service.create_project(name="cascade-scope", owner=owner)
        obj = await _ingest(project, owner=owner, content=uuid.uuid4().bytes)
        digest = obj.blob_sha256
        assert digest is not None
        before = await Blob.get(digest)
        assert before is not None

        await project_service.delete_project_tree(project.id, owner=owner)

        # detach_blob_from_object deletes the object row and decrements the
        # blob in one transaction; the bytes stay until the grace-windowed
        # gc_blobs job, so the post-condition is refcount 0, not an absent row.
        assert await DataObject.get(obj.id) is None
        after = await Blob.get(digest)
        assert after is not None
        assert after.ref_count == before.ref_count - 1
