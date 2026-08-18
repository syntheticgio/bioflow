"""The chunked-upload session and route layer.

`tests/storage/test_upload_chunks.py` covers chunk sizing and the atomic
`.part` write; what it does not touch is the HTTP layer above them -- the
session lifecycle from POST through PUT chunks to complete, and the refusals
along the way.

Two pieces of real infrastructure are needed rather than mocked, because both
are what the routes actually fail on. `create_session` calls `require_home()`,
so `bioinfo_home` is pointed at a tmp_path carrying the `.biopipe/VERSION`
sentinel `check_home` looks for -- without it every POST is a 503 about an
unmounted drive rather than the case under test. And `complete_session`
enqueues assembly through Redis, stubbed the way `test_exports_api.py` stubs
it, since this process has no Redis.
"""

import hashlib

import pytest
import pytest_asyncio

from app.config import settings
from app.models import UploadSession, UploadState
from app.queue import queue
from app.services import upload_service
from tests.services.helpers import make_project

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    """A writable BIOINFO_HOME with the mount sentinel present."""
    home = tmp_path / "bioinfo"
    (home / ".biopipe").mkdir(parents=True)
    (home / ".biopipe" / "VERSION").write_text("1")
    monkeypatch.setattr(settings, "bioinfo_home", home)
    return home


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    async def _skip(*args, **kwargs):
        return None

    monkeypatch.setattr(queue, "_push_to_redis", _skip)
    monkeypatch.setattr(queue, "publish_event", _skip)


@pytest_asyncio.fixture(loop_scope="module")
async def a_project(two_profiles):
    return await make_project("uploads", owner=two_profiles["a"].owner_id())


async def _open_session(client, headers, project, *, filename="reads.fastq", size=10):
    resp = await client.post(
        "/api/v1/uploads",
        json={"project_id": str(project.id), "filename": filename, "total_size": size},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["session"]


class TestCreate:
    async def test_opens_a_session(self, client, two_profiles, a_project):
        resp = await client.post(
            "/api/v1/uploads",
            json={
                "project_id": str(a_project.id),
                "filename": "reads.fastq",
                "total_size": 1024,
            },
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["dedup_hit"] is False
        assert body["session"]["filename"] == "reads.fastq"
        assert body["session"]["state"] == "open"
        assert body["session"]["total_chunks"] >= 1
        assert body["object"] is None

    async def test_reports_which_chunks_are_still_missing(
        self, client, two_profiles, a_project
    ):
        """A fresh session is missing all of them; this is the field a resuming
        client reads to know where to start."""
        session = await _open_session(client, two_profiles["a_headers"], a_project)

        assert session["missing_chunks"] == list(range(session["total_chunks"]))
        assert session["received_chunks"] == 0

    async def test_creates_the_staging_directory(
        self, client, two_profiles, a_project, _home
    ):
        session = await _open_session(client, two_profiles["a_headers"], a_project)

        assert (_home / "staging" / session["id"] / "chunks").is_dir()

    async def test_404s_for_another_profiles_project(
        self, client, two_profiles, a_project
    ):
        resp = await client.post(
            "/api/v1/uploads",
            json={
                "project_id": str(a_project.id),
                "filename": "reads.fastq",
                "total_size": 10,
            },
            headers=two_profiles["b_headers"],
        )

        assert resp.status_code == 404, resp.text

    async def test_404s_for_a_missing_project(self, client, two_profiles):
        resp = await client.post(
            "/api/v1/uploads",
            json={
                "project_id": "000000000000000000000000",
                "filename": "reads.fastq",
                "total_size": 10,
            },
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 404

    @pytest.mark.parametrize("filename", ["", ".", "..", "/"])
    async def test_refuses_an_unsafe_filename(
        self, client, two_profiles, a_project, filename
    ):
        """`Path(filename).name` reduces a traversal to its last segment, and
        what is left has to be rejected rather than stored as a session whose
        assembled object has no usable name."""
        resp = await client.post(
            "/api/v1/uploads",
            json={
                "project_id": str(a_project.id),
                "filename": filename,
                "total_size": 10,
            },
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 422, resp.text

    async def test_strips_a_traversal_prefix_from_a_real_filename(
        self, client, two_profiles, a_project
    ):
        session = await _open_session(
            client,
            two_profiles["a_headers"],
            a_project,
            filename="../../etc/passwd",
        )

        assert session["filename"] == "passwd"

    async def test_refuses_a_negative_total_size(self, client, two_profiles, a_project):
        resp = await client.post(
            "/api/v1/uploads",
            json={
                "project_id": str(a_project.id),
                "filename": "reads.fastq",
                "total_size": -1,
            },
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 422, resp.text


class TestList:
    async def test_lists_the_callers_open_sessions(
        self, client, two_profiles, a_project
    ):
        session = await _open_session(client, two_profiles["a_headers"], a_project)

        resp = await client.get("/api/v1/uploads", headers=two_profiles["a_headers"])

        assert resp.status_code == 200, resp.text
        assert session["id"] in [s["id"] for s in resp.json()]

    async def test_does_not_list_another_profiles_sessions(
        self, client, two_profiles, a_project
    ):
        session = await _open_session(client, two_profiles["a_headers"], a_project)

        resp = await client.get("/api/v1/uploads", headers=two_profiles["b_headers"])

        assert session["id"] not in [s["id"] for s in resp.json()]

    async def test_rejects_a_limit_over_the_cap(self, client, two_profiles):
        resp = await client.get(
            "/api/v1/uploads?limit=10000", headers=two_profiles["a_headers"]
        )

        assert resp.status_code == 422


class TestGet:
    async def test_returns_the_session(self, client, two_profiles, a_project):
        session = await _open_session(client, two_profiles["a_headers"], a_project)

        resp = await client.get(
            f"/api/v1/uploads/{session['id']}", headers=two_profiles["a_headers"]
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["id"] == session["id"]

    async def test_404s_for_another_profiles_session(
        self, client, two_profiles, a_project
    ):
        session = await _open_session(client, two_profiles["a_headers"], a_project)

        resp = await client.get(
            f"/api/v1/uploads/{session['id']}", headers=two_profiles["b_headers"]
        )

        assert resp.status_code == 404

    async def test_404s_for_a_missing_session(self, client, two_profiles):
        resp = await client.get(
            "/api/v1/uploads/000000000000000000000000",
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 404


class TestPutChunk:
    async def test_accepts_a_chunk_and_reports_progress(
        self, client, two_profiles, a_project
    ):
        session = await _open_session(client, two_profiles["a_headers"], a_project)
        payload = b"ACGT" * 2 + b"AC"

        resp = await client.put(
            f"/api/v1/uploads/{session['id']}/chunks/0",
            content=payload,
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["index"] == 0
        assert body["received_chunks"] == 1
        assert body["missing_count"] == session["total_chunks"] - 1
        assert body["received_bytes"] == len(payload)

    async def test_re_sending_a_chunk_is_safe(self, client, two_profiles, a_project):
        """Re-sending is normal on a flaky link, so it must not double-count."""
        session = await _open_session(client, two_profiles["a_headers"], a_project)
        payload = b"ACGTACGTAC"

        for _ in range(2):
            resp = await client.put(
                f"/api/v1/uploads/{session['id']}/chunks/0",
                content=payload,
                headers=two_profiles["a_headers"],
            )

        assert resp.json()["received_chunks"] == 1
        assert resp.json()["received_bytes"] == len(payload)

    async def test_refuses_an_empty_body(self, client, two_profiles, a_project):
        session = await _open_session(client, two_profiles["a_headers"], a_project)

        resp = await client.put(
            f"/api/v1/uploads/{session['id']}/chunks/0",
            content=b"",
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 422, resp.text

    async def test_refuses_an_out_of_range_index(self, client, two_profiles, a_project):
        session = await _open_session(client, two_profiles["a_headers"], a_project)

        resp = await client.put(
            f"/api/v1/uploads/{session['id']}/chunks/{session['total_chunks']}",
            content=b"ACGT",
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 422, resp.text

    async def test_accepts_a_chunk_whose_digest_matches_the_header(
        self, client, two_profiles, a_project
    ):
        session = await _open_session(client, two_profiles["a_headers"], a_project)
        payload = b"ACGTACGTAC"

        resp = await client.put(
            f"/api/v1/uploads/{session['id']}/chunks/0",
            content=payload,
            headers={
                **two_profiles["a_headers"],
                "X-Chunk-SHA256": hashlib.sha256(payload).hexdigest(),
            },
        )

        assert resp.status_code == 200, resp.text

    async def test_refuses_a_chunk_whose_digest_does_not_match(
        self, client, two_profiles, a_project
    ):
        """The digest is the only thing standing between a corrupted chunk and a
        completed object that fails verification days later."""
        session = await _open_session(client, two_profiles["a_headers"], a_project)

        resp = await client.put(
            f"/api/v1/uploads/{session['id']}/chunks/0",
            content=b"ACGTACGTAC",
            headers={**two_profiles["a_headers"], "X-Chunk-SHA256": "00" * 32},
        )

        assert resp.status_code == 422, resp.text

    async def test_404s_writing_into_another_profiles_session(
        self, client, two_profiles, a_project
    ):
        """The sharpest case in the router: an unscoped chunk write does not
        read someone else's data, it writes bytes into the file they are
        assembling."""
        session = await _open_session(client, two_profiles["a_headers"], a_project)

        resp = await client.put(
            f"/api/v1/uploads/{session['id']}/chunks/0",
            content=b"ACGTACGTAC",
            headers=two_profiles["b_headers"],
        )

        assert resp.status_code == 404, resp.text


class TestComplete:
    async def test_completes_a_fully_uploaded_session(
        self, client, two_profiles, a_project
    ):
        session = await _open_session(client, two_profiles["a_headers"], a_project)
        await client.put(
            f"/api/v1/uploads/{session['id']}/chunks/0",
            content=b"ACGTACGTAC",
            headers=two_profiles["a_headers"],
        )

        resp = await client.post(
            f"/api/v1/uploads/{session['id']}/complete",
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["session_id"] == session["id"]
        assert body["object_id"]

    async def test_409s_while_chunks_are_still_missing(
        self, client, two_profiles, a_project
    ):
        session = await _open_session(client, two_profiles["a_headers"], a_project)

        resp = await client.post(
            f"/api/v1/uploads/{session['id']}/complete",
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 409, resp.text
        assert resp.json()["details"]["missing_count"] == session["total_chunks"]

    async def test_404s_for_another_profiles_session(
        self, client, two_profiles, a_project
    ):
        session = await _open_session(client, two_profiles["a_headers"], a_project)

        resp = await client.post(
            f"/api/v1/uploads/{session['id']}/complete",
            headers=two_profiles["b_headers"],
        )

        assert resp.status_code == 404


class TestAbort:
    async def test_aborts_the_session_and_purges_staging(
        self, client, two_profiles, a_project, _home
    ):
        session = await _open_session(client, two_profiles["a_headers"], a_project)
        staging = _home / "staging" / session["id"]
        assert staging.is_dir()

        resp = await client.delete(
            f"/api/v1/uploads/{session['id']}", headers=two_profiles["a_headers"]
        )

        assert resp.status_code == 204, resp.text
        assert not staging.exists()
        stored = await UploadSession.get(session["id"])
        assert stored.state is UploadState.ABORTED

    async def test_an_aborted_session_refuses_further_chunks(
        self, client, two_profiles, a_project
    ):
        session = await _open_session(client, two_profiles["a_headers"], a_project)
        await client.delete(
            f"/api/v1/uploads/{session['id']}", headers=two_profiles["a_headers"]
        )

        resp = await client.put(
            f"/api/v1/uploads/{session['id']}/chunks/0",
            content=b"ACGTACGTAC",
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 409, resp.text

    async def test_404s_for_another_profiles_session(
        self, client, two_profiles, a_project
    ):
        session = await _open_session(client, two_profiles["a_headers"], a_project)

        resp = await client.delete(
            f"/api/v1/uploads/{session['id']}", headers=two_profiles["b_headers"]
        )

        assert resp.status_code == 404


class TestMissingChunksCap:
    async def test_the_missing_list_is_capped_for_a_huge_upload(
        self, client, two_profiles, a_project
    ):
        """A client resuming a 10,000-chunk upload does not need the whole list
        in one response to make progress."""
        session = await _open_session(
            client,
            two_profiles["a_headers"],
            a_project,
            filename="huge.fastq",
            size=upload_service.DEFAULT_CHUNK_SIZE * 900,
        )

        assert session["total_chunks"] == 900
        assert len(session["missing_chunks"]) == 500
