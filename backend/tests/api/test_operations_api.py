"""Project-level operations: merge-fastq, batch-rename, batch-tags, export, qc-all.

These are the actions that do not start from a selected file, and merge-fastq
is the one with real teeth: it reads blobs off disk, concatenates them, and
re-ingests the result. It is tested against real bytes in a real
`bioinfo_home` rather than a mocked object service, because every refusal in
that route is about the state of something on disk -- a missing blob, a
non-FASTQ input, an object still ingesting -- and a mock cannot be wrong about
those in the way the filesystem can.

`launch_qc` and `ingest_stream` both reach the queue, which pushes to Redis;
stubbed as `test_exports_api.py` stubs it, since this process has no Redis.
"""

import gzip
import hashlib

import pytest
import pytest_asyncio

from app.config import settings
from app.models import Blob, BlobStorage, DataObject, FormatInfo, FormatKind, ObjectStatus
from app.queue import queue
from app.storage.paths import blob_path
from tests.services.helpers import make_project

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
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
    return await make_project("operations", owner=two_profiles["a"].owner_id())


async def _stored_fastq(
    project,
    name,
    content: bytes,
    *,
    kind: FormatKind = FormatKind.FASTQ,
    status: ObjectStatus = ObjectStatus.READY,
    on_disk: bool = True,
) -> DataObject:
    """An object whose blob really exists under `objects_dir`.

    `make_object` in tests/services/helpers.py fabricates a digest and never
    writes bytes, which is right for the deletion-cascade tests it serves and
    wrong here: merge-fastq opens every input path.
    """
    digest = hashlib.sha256(content).hexdigest()
    if await Blob.get(digest) is None:
        await Blob(
            id=digest, size=len(content), ref_count=1, storage=BlobStorage.MANAGED
        ).insert()
    if on_disk:
        path = blob_path(digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    obj = DataObject(
        project_id=project.id,
        owner=project.owner,
        name=name,
        size=len(content),
        blob_sha256=digest,
        status=status,
        format=FormatInfo(kind=kind),
    )
    await obj.insert()
    return obj


READ_A = b"@r1\nACGT\n+\nIIII\n"
READ_B = b"@r2\nTGCA\n+\nIIII\n"


class TestMergeFastq:
    async def test_concatenates_the_inputs_into_a_new_object(
        self, client, two_profiles, a_project
    ):
        one = await _stored_fastq(a_project, "R1_L001.fastq", READ_A)
        two = await _stored_fastq(a_project, "R1_L002.fastq", READ_B)

        resp = await client.post(
            f"/api/v1/projects/{a_project.id}/operations/merge-fastq",
            json={
                "object_ids": [str(one.id), str(two.id)],
                "output_name": "R1_merged.fastq",
            },
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 202, resp.text
        body = resp.json()
        # `.gz`: re-ingesting goes through `ingest_stream`, which compresses on
        # the way in, so the stored name is not quite the requested one.
        assert body["name"] == "R1_merged.fastq.gz"
        merged = await DataObject.get(body["object_id"])
        # Content, not `size`: the reported size is of the compressed blob, so
        # the round trip through gzip is the only assertion that says the two
        # inputs arrived intact and in order.
        assert gzip.decompress(blob_path(merged.blob_sha256).read_bytes()) == (
            READ_A + READ_B
        )

    async def test_leaves_no_temp_file_behind(self, client, two_profiles, a_project):
        """The merge stages into /tmp before re-ingesting; a multi-GB leak there
        is the kind that fills a disk quietly."""
        from pathlib import Path

        one = await _stored_fastq(a_project, "tmp_a.fastq", READ_A)
        two = await _stored_fastq(a_project, "tmp_b.fastq", READ_B)

        await client.post(
            f"/api/v1/projects/{a_project.id}/operations/merge-fastq",
            json={
                "object_ids": [str(one.id), str(two.id)],
                "output_name": "tmp_merged.fastq",
            },
            headers=two_profiles["a_headers"],
        )

        assert not (Path("/tmp/bioflow-merge-fastq") / "tmp_merged.fastq").exists()

    async def test_refuses_an_object_that_is_not_in_the_project(
        self, client, two_profiles, a_project
    ):
        elsewhere = await make_project("other", owner=two_profiles["a"].owner_id())
        one = await _stored_fastq(a_project, "here.fastq", READ_A)
        two = await _stored_fastq(elsewhere, "there.fastq", READ_B)

        resp = await client.post(
            f"/api/v1/projects/{a_project.id}/operations/merge-fastq",
            json={
                "object_ids": [str(one.id), str(two.id)],
                "output_name": "merged.fastq",
            },
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 422, resp.text
        assert "not found in project" in resp.json()["message"]

    async def test_refuses_another_profiles_objects(
        self, client, two_profiles, a_project
    ):
        """`get_objects_by_ids` filters on owner, so B's ids read as absent
        rather than as someone else's files."""
        b_project = await make_project("b side", owner=two_profiles["b"].owner_id())
        one = await _stored_fastq(b_project, "b1.fastq", READ_A)
        two = await _stored_fastq(b_project, "b2.fastq", READ_B)

        resp = await client.post(
            f"/api/v1/projects/{b_project.id}/operations/merge-fastq",
            json={
                "object_ids": [str(one.id), str(two.id)],
                "output_name": "merged.fastq",
            },
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 422, resp.text

    async def test_refuses_a_non_fastq_input(self, client, two_profiles, a_project):
        one = await _stored_fastq(a_project, "reads.fastq", READ_A)
        two = await _stored_fastq(
            a_project, "ref.fasta", b">c\nACGT\n", kind=FormatKind.FASTA
        )

        resp = await client.post(
            f"/api/v1/projects/{a_project.id}/operations/merge-fastq",
            json={
                "object_ids": [str(one.id), str(two.id)],
                "output_name": "merged.fastq",
            },
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 422, resp.text
        assert "not a FASTQ" in resp.json()["message"]

    async def test_refuses_an_input_that_is_not_ready(
        self, client, two_profiles, a_project
    ):
        """A file still ingesting has no complete blob to concatenate."""
        one = await _stored_fastq(a_project, "ready.fastq", READ_A)
        two = await _stored_fastq(
            a_project, "pending.fastq", READ_B, status=ObjectStatus.INGESTING
        )

        resp = await client.post(
            f"/api/v1/projects/{a_project.id}/operations/merge-fastq",
            json={
                "object_ids": [str(one.id), str(two.id)],
                "output_name": "merged.fastq",
            },
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 422, resp.text
        assert "not ready" in resp.json()["message"]

    async def test_refuses_when_a_blob_is_missing_from_disk(
        self, client, two_profiles, a_project
    ):
        one = await _stored_fastq(a_project, "present.fastq", READ_A)
        two = await _stored_fastq(a_project, "absent.fastq", READ_B, on_disk=False)

        resp = await client.post(
            f"/api/v1/projects/{a_project.id}/operations/merge-fastq",
            json={
                "object_ids": [str(one.id), str(two.id)],
                "output_name": "merged.fastq",
            },
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 422, resp.text
        assert "Blob not found on disk" in resp.json()["message"]

    async def test_refuses_fewer_than_two_inputs(self, client, two_profiles, a_project):
        """Merging one file is a copy, not a merge; the model declares
        `min_length=2` and this is what makes that reach the caller as a 422."""
        one = await _stored_fastq(a_project, "lonely.fastq", READ_A)

        resp = await client.post(
            f"/api/v1/projects/{a_project.id}/operations/merge-fastq",
            json={"object_ids": [str(one.id)], "output_name": "merged.fastq"},
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 422

    async def test_refuses_an_empty_output_name(self, client, two_profiles, a_project):
        resp = await client.post(
            f"/api/v1/projects/{a_project.id}/operations/merge-fastq",
            json={
                "object_ids": ["a" * 24, "b" * 24],
                "output_name": "",
            },
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 422


class TestBatchRename:
    async def test_renames_the_objects(self, client, two_profiles, a_project):
        one = await _stored_fastq(a_project, "old_a.fastq", READ_A)
        two = await _stored_fastq(a_project, "old_b.fastq", READ_B)

        resp = await client.post(
            f"/api/v1/projects/{a_project.id}/operations/batch-rename",
            json={
                "renames": [
                    {"id": str(one.id), "name": "new_a.fastq"},
                    {"id": str(two.id), "name": "new_b.fastq"},
                ]
            },
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"updated": 2}
        await one.sync()
        await two.sync()
        assert (one.name, two.name) == ("new_a.fastq", "new_b.fastq")

    async def test_skips_an_entry_missing_its_id_or_name(
        self, client, two_profiles, a_project
    ):
        """Incomplete entries are skipped rather than refused, so the count in
        the response is the only way a caller learns some were dropped."""
        one = await _stored_fastq(a_project, "partial.fastq", READ_A)

        resp = await client.post(
            f"/api/v1/projects/{a_project.id}/operations/batch-rename",
            json={
                "renames": [
                    {"id": str(one.id), "name": "renamed.fastq"},
                    {"id": str(one.id)},
                    {"name": "orphan.fastq"},
                ]
            },
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"updated": 1}

    async def test_404s_on_another_profiles_object(
        self, client, two_profiles, a_project
    ):
        b_project = await make_project("b rename", owner=two_profiles["b"].owner_id())
        theirs = await _stored_fastq(b_project, "theirs.fastq", READ_A)

        resp = await client.post(
            f"/api/v1/projects/{a_project.id}/operations/batch-rename",
            json={"renames": [{"id": str(theirs.id), "name": "stolen.fastq"}]},
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 404, resp.text
        await theirs.sync()
        assert theirs.name == "theirs.fastq"

    async def test_refuses_an_empty_rename_list(self, client, two_profiles, a_project):
        resp = await client.post(
            f"/api/v1/projects/{a_project.id}/operations/batch-rename",
            json={"renames": []},
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 422


class TestBatchTags:
    async def test_adds_tags_across_objects(self, client, two_profiles, a_project):
        one = await _stored_fastq(a_project, "tag_a.fastq", READ_A)
        two = await _stored_fastq(a_project, "tag_b.fastq", READ_B)

        resp = await client.post(
            f"/api/v1/projects/{a_project.id}/operations/batch-tags",
            json={"object_ids": [str(one.id), str(two.id)], "add": ["lane1"]},
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["updated"]["modified"] == 2
        await one.sync()
        assert one.tags == ["lane1"]

    async def test_removes_a_tag(self, client, two_profiles, a_project):
        one = await _stored_fastq(a_project, "untag.fastq", READ_A)
        one.tags = ["keep", "drop"]
        await one.save()

        resp = await client.post(
            f"/api/v1/projects/{a_project.id}/operations/batch-tags",
            json={"object_ids": [str(one.id)], "remove": ["drop"]},
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 200, resp.text
        await one.sync()
        assert one.tags == ["keep"]

    async def test_404s_when_one_id_is_another_profiles(
        self, client, two_profiles, a_project
    ):
        b_project = await make_project("b tags", owner=two_profiles["b"].owner_id())
        mine = await _stored_fastq(a_project, "tags_mine.fastq", READ_A)
        theirs = await _stored_fastq(b_project, "tags_theirs.fastq", READ_B)

        resp = await client.post(
            f"/api/v1/projects/{a_project.id}/operations/batch-tags",
            json={"object_ids": [str(mine.id), str(theirs.id)], "add": ["x"]},
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 404, resp.text
        await mine.sync()
        assert mine.tags == []


class TestExport:
    async def test_summarizes_the_project(self, client, two_profiles):
        project = await make_project(
            "summary target", owner=two_profiles["a"].owner_id()
        )
        await _stored_fastq(project, "s1.fastq", READ_A)
        await _stored_fastq(
            project, "s2.fasta", b">c\nACGT\n", kind=FormatKind.FASTA
        )

        resp = await client.get(
            f"/api/v1/projects/{project.id}/operations/export",
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["project_name"] == "summary target"
        assert body["total_files"] == 2
        assert body["total_bytes"] == len(READ_A) + len(b">c\nACGT\n")
        assert body["files_by_format"] == {"fastq": 1, "fasta": 1}
        assert body["files_by_status"] == {"ready": 2}

    async def test_an_empty_project_summarizes_to_zeroes(self, client, two_profiles):
        project = await make_project("empty", owner=two_profiles["a"].owner_id())

        resp = await client.get(
            f"/api/v1/projects/{project.id}/operations/export",
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total_files"] == 0
        assert body["total_bytes"] == 0
        assert body["files_by_format"] == {}

    async def test_404s_for_another_profiles_project(self, client, two_profiles):
        project = await make_project("not yours", owner=two_profiles["a"].owner_id())

        resp = await client.get(
            f"/api/v1/projects/{project.id}/operations/export",
            headers=two_profiles["b_headers"],
        )

        assert resp.status_code == 404

    async def test_404s_for_a_missing_project(self, client, two_profiles):
        resp = await client.get(
            "/api/v1/projects/000000000000000000000000/operations/export",
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 404


class TestQcAll:
    async def test_queues_one_job_per_ready_fastq(self, client, two_profiles):
        project = await make_project("qc all", owner=two_profiles["a"].owner_id())
        await _stored_fastq(project, "q1.fastq", READ_A)
        await _stored_fastq(project, "q2.fastq", READ_B)

        resp = await client.post(
            f"/api/v1/projects/{project.id}/operations/qc-all",
            json={},
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["count"] == 2
        assert len(body["job_ids"]) == 2

    async def test_skips_files_that_are_not_ready_fastq(self, client, two_profiles):
        """QC runs over reads; a reference FASTA and a half-ingested file are
        both silently out of scope rather than errors."""
        project = await make_project("qc mixed", owner=two_profiles["a"].owner_id())
        await _stored_fastq(project, "reads.fastq", READ_A)
        await _stored_fastq(project, "ref.fasta", b">c\nACGT\n", kind=FormatKind.FASTA)
        await _stored_fastq(
            project, "half.fastq", READ_B, status=ObjectStatus.INGESTING
        )

        resp = await client.post(
            f"/api/v1/projects/{project.id}/operations/qc-all",
            json={},
            headers=two_profiles["a_headers"],
        )

        assert resp.json()["count"] == 1

    async def test_reports_a_conflict_per_object_rather_than_failing_the_batch(
        self, client, two_profiles, monkeypatch
    ):
        """One file already having a QC job queued must not stop the rest of the
        project from being submitted."""
        from app.errors import ConflictError
        from app.services import pipeline_service

        project = await make_project("qc conflict", owner=two_profiles["a"].owner_id())
        await _stored_fastq(project, "c1.fastq", READ_A)

        async def _always_conflicts(*args, **kwargs):
            raise ConflictError("An identical QC job is already queued")

        monkeypatch.setattr(pipeline_service, "launch_qc", _always_conflicts)

        resp = await client.post(
            f"/api/v1/projects/{project.id}/operations/qc-all",
            json={},
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["count"] == 0
        assert len(body["errors"]) == 1

    async def test_an_empty_project_queues_nothing(self, client, two_profiles):
        project = await make_project("qc empty", owner=two_profiles["a"].owner_id())

        resp = await client.post(
            f"/api/v1/projects/{project.id}/operations/qc-all",
            json={},
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 202, resp.text
        assert resp.json() == {"job_ids": [], "count": 0}

    async def test_queues_nothing_from_another_profiles_project(
        self, client, two_profiles
    ):
        """Note this is a 202-with-nothing rather than the 404 `/export` on the
        same router gives, because `list_objects` filters by owner without ever
        asking whether the project exists. Nothing leaks either way -- B learns
        no filename -- but the two routes disagree about what a wrong-owner
        project is, and this test pins the behaviour that ships rather than the
        one the neighbouring route would suggest.
        """
        project = await make_project("qc theirs", owner=two_profiles["a"].owner_id())
        await _stored_fastq(project, "hidden.fastq", READ_A)

        resp = await client.post(
            f"/api/v1/projects/{project.id}/operations/qc-all",
            json={},
            headers=two_profiles["b_headers"],
        )

        assert resp.status_code == 202, resp.text
        assert resp.json() == {"job_ids": [], "count": 0}
