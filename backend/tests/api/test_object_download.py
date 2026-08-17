"""Serving an object's raw stored bytes.

The handler is awaited directly rather than driven through TestClient. That is
not a shortcut: TestClient runs the app on its own event loop via a blocking
portal, while `beanie_models` holds a Motor connection bound to the test's
loop, and mixing the two fails with "attached to a different loop" before any
assertion runs. Calling the coroutine keeps the database work on one loop, and
what matters here -- which file gets resolved, under what name -- is decided in
the handler, not in the HTTP layer above it.

Real documents and real files on both sides, because the thing worth testing is
that the route hands back the user's own file byte for byte, for both storage
kinds. A test that reimplemented the path join would keep passing after the
route stopped resolving blobs that way.
"""

import pytest
import pytest_asyncio
from app.api.v1.objects import download_object
from app.config import settings
from app.errors import NotFoundError
from app.models import Blob, BlobState, BlobStorage, DataObject
from app.storage.paths import blob_rel_path
from beanie import PydanticObjectId

# `beanie_models` is module-scoped and holds a Motor connection bound to that
# scope's loop, so the tests have to run on the same one. Without the explicit
# loop_scope, pytest-asyncio gives each test a fresh function-scoped loop and
# every query fails with "attached to a different loop".
pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]

PROJECT_ID = PydanticObjectId("507f191e810c19729de860ea")

# The fixture's objects carry TimestampedDocument's "local" default, so this is
# the owner the route must resolve for them to be visible at all.
OWNER = "local"

# Content-addressed: the managed file has to sit at the path the digest names,
# because that derivation is what the route relies on.
MANAGED_BYTES = b"@read1\nACGT\n+\nIIII\n"
EXTERNAL_BYTES = b"@ext\nTTTT\n+\nIIII\n"
MANAGED_SHA = "0e2b0b7d4b0c0e1a3f2c9d8e7a6b5c4d3e2f1a0b9c8d7e6f5a4b3c2d1e0f9a8b"
EXTERNAL_SHA = "1f3c1c8e5c1d1f2b4a3d0e9f8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1a0b9c"


@pytest_asyncio.fixture(loop_scope="module")
async def objects(tmp_path_factory, monkeypatch):
    """A managed object and an external one, with bytes actually on disk."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(settings, "bioinfo_home", home)

    managed_file = home / "objects" / blob_rel_path(MANAGED_SHA)
    managed_file.parent.mkdir(parents=True, exist_ok=True)
    managed_file.write_bytes(MANAGED_BYTES)

    external_file = home / "drive" / "sample_R1.fastq"
    external_file.parent.mkdir(parents=True, exist_ok=True)
    external_file.write_bytes(EXTERNAL_BYTES)

    await Blob(
        id=MANAGED_SHA,
        size=len(MANAGED_BYTES),
        state=BlobState.PRESENT,
        storage=BlobStorage.MANAGED,
        rel_path=blob_rel_path(MANAGED_SHA),
        ref_count=1,
    ).insert()
    await Blob(
        id=EXTERNAL_SHA,
        size=len(EXTERNAL_BYTES),
        state=BlobState.PRESENT,
        storage=BlobStorage.EXTERNAL,
        external_path=str(external_file),
        ref_count=1,
    ).insert()

    managed = await DataObject(
        project_id=PROJECT_ID, name="reads_R1.fastq", blob_sha256=MANAGED_SHA
    ).insert()
    external = await DataObject(
        project_id=PROJECT_ID, name="on_the_drive.fastq", blob_sha256=EXTERNAL_SHA
    ).insert()
    pending = await DataObject(
        project_id=PROJECT_ID, name="still_uploading.fastq"
    ).insert()

    yield {
        "managed": managed.id,
        "external": external.id,
        "pending": pending.id,
        "external_file": external_file,
    }

    for doc in (managed, external, pending):
        await doc.delete()
    for digest in (MANAGED_SHA, EXTERNAL_SHA):
        blob = await Blob.get(digest)
        if blob:
            await blob.delete()


class TestServingContent:
    async def test_serves_a_managed_blob(self, objects):
        r = await download_object(objects["managed"], OWNER)
        assert r.path.read_bytes() == MANAGED_BYTES

    async def test_serves_an_external_blob_from_where_it_lives(self, objects):
        """External blobs are registered in place and never copied into the
        store, so the route must follow external_path rather than derive a
        path from the digest."""
        r = await download_object(objects["external"], OWNER)
        assert r.path.read_bytes() == EXTERNAL_BYTES
        assert str(r.path) == str(objects["external_file"])

    async def test_downloads_under_the_users_filename(self, objects):
        """Not the digest: getting your own file back is the entire point."""
        r = await download_object(objects["managed"], OWNER)
        assert r.filename == "reads_R1.fastq"
        assert "attachment" in r.headers["content-disposition"]
        assert "reads_R1.fastq" in r.headers["content-disposition"]

    async def test_content_type_is_opaque_and_not_sniffed(self, objects):
        """These are payloads for the user's own tools. Letting a browser pick
        a renderable type for bytes that came from outside is how a download
        turns into a page."""
        r = await download_object(objects["managed"], OWNER)
        assert r.media_type == "application/octet-stream"
        assert r.headers["x-content-type-options"] == "nosniff"


class TestUnavailableContent:
    async def test_an_object_with_no_blob_yet_is_a_404(self, objects):
        """Still uploading, or hashing never finished."""
        with pytest.raises(NotFoundError):
            await download_object(objects["pending"], OWNER)

    async def test_a_vanished_external_file_is_a_404(self, objects):
        """An unmounted drive is the ordinary case. It has to fail as a clean
        404 rather than raising once the response has already begun."""
        objects["external_file"].unlink()
        with pytest.raises(NotFoundError):
            await download_object(objects["external"], OWNER)

    async def test_an_unknown_object_is_a_404(self, objects):
        with pytest.raises(NotFoundError):
            await download_object(PydanticObjectId("507f1f77bcf86cd799439011"), OWNER)

    async def test_another_profile_cannot_download_the_bytes(self, objects):
        """The one that fails if the route resolves an owner and ignores it.

        Everything above passes whether or not the filter reaches the query --
        the objects belong to the owner being asked for. Only a request under a
        *different* owner distinguishes a scoped route from a hardcoded one,
        and it must not hand back the file.
        """
        with pytest.raises(NotFoundError):
            await download_object(objects["managed"], "someone-else")
