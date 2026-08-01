"""The owner partition, proven over HTTP for every router that resolves it.

`tests/api/test_profiles.py::TestOwnerPartition` does this for the two project
routes that were wired first. This module covers the rest -- objects, runs,
pipelines, uploads -- because the failure being guarded against is per-route,
not per-feature: a router can resolve `OwnerDep` correctly and still hand the
service a hardcoded owner, and nothing below the route layer notices. The
service tests all pass an owner directly, so they cannot see it either.

Every assertion here is about what profile B *cannot* reach. A test that only
checks A seeing A's own data passes against a route that ignores the header
entirely, which is precisely the bug this module exists to catch -- see
CLAUDE.md on asserting the direction that fails when the seam breaks.
"""

import pytest
from beanie import PydanticObjectId

from app.models import (
    DataObject,
    FormatInfo,
    FormatKind,
    Job,
    JobState,
    ObjectStatus,
    RunKind,
    UploadSession,
    UploadState,
)
from app.services import object_service, project_service, run_service, upload_service

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


async def _project(owner: str, name: str):
    return await project_service.create_project(name=name, owner=owner)


async def _object(
    owner: str,
    project_id: PydanticObjectId,
    name: str,
    *,
    fmt_kind: FormatKind = FormatKind.UNKNOWN,
    status: ObjectStatus = ObjectStatus.UPLOADING,
) -> DataObject:
    """A bare object row.

    Inserted directly rather than through an ingest: what is under test is the
    route's owner filter, and going through the ingest path would drag a blob,
    a digest and a queue connection into a test about a query.

    `fmt_kind`/`status` exist for the reference listing, which filters on both
    before the owner filter is even observable -- an object left at the
    defaults would be absent from that response for the wrong reason.
    """
    obj = DataObject(
        project_id=project_id,
        name=name,
        owner=owner,
        format=FormatInfo(kind=fmt_kind),
        status=status,
    )
    await obj.insert()
    return obj


class TestObjectsRouter:
    async def test_another_profile_cannot_read_an_object(self, client, two_profiles):
        project = await _project(two_profiles["a"].owner_id(), "a-objects")
        obj = await _object(two_profiles["a"].owner_id(), project.id, "reads.fastq")

        mine = await client.get(f"/api/v1/objects/{obj.id}", headers=two_profiles["a_headers"])
        theirs = await client.get(
            f"/api/v1/objects/{obj.id}", headers=two_profiles["b_headers"]
        )

        assert mine.status_code == 200
        assert theirs.status_code == 404

    async def test_another_profile_cannot_patch_an_object(self, client, two_profiles):
        """A write, so a leak here corrupts rather than merely discloses."""
        project = await _project(two_profiles["a"].owner_id(), "a-patch")
        obj = await _object(two_profiles["a"].owner_id(), project.id, "reads.fastq")

        r = await client.patch(
            f"/api/v1/objects/{obj.id}",
            json={"name": "renamed-by-b.fastq"},
            headers=two_profiles["b_headers"],
        )

        assert r.status_code == 404
        assert (await DataObject.get(obj.id)).name == "reads.fastq"

    async def test_another_profile_cannot_delete_an_object(self, client, two_profiles):
        project = await _project(two_profiles["a"].owner_id(), "a-delete")
        obj = await _object(two_profiles["a"].owner_id(), project.id, "reads.fastq")

        r = await client.delete(
            f"/api/v1/objects/{obj.id}", headers=two_profiles["b_headers"]
        )

        assert r.status_code == 404
        assert await DataObject.get(obj.id) is not None

    async def test_objects_require_a_profile_header(self, client, two_profiles):
        project = await _project(two_profiles["a"].owner_id(), "a-headerless")
        obj = await _object(two_profiles["a"].owner_id(), project.id, "reads.fastq")

        r = await client.get(f"/api/v1/objects/{obj.id}")

        assert r.status_code == 400
        assert r.json()["code"] == "profile_unresolved"


class TestProjectsRouter:
    async def test_another_profile_cannot_read_a_project(self, client, two_profiles):
        project = await _project(two_profiles["a"].owner_id(), "a-detail")

        r = await client.get(
            f"/api/v1/projects/{project.id}", headers=two_profiles["b_headers"]
        )

        assert r.status_code == 404

    async def test_another_profile_cannot_list_a_projects_objects(
        self, client, two_profiles
    ):
        """Two filters have to hold, not one.

        The route checks the project *and* scopes the object listing. Asserting
        the 404 alone would still pass if only the first were wired.
        """
        project = await _project(two_profiles["a"].owner_id(), "a-listing")
        await _object(two_profiles["a"].owner_id(), project.id, "reads.fastq")

        mine = await client.get(
            f"/api/v1/projects/{project.id}/objects", headers=two_profiles["a_headers"]
        )
        theirs = await client.get(
            f"/api/v1/projects/{project.id}/objects", headers=two_profiles["b_headers"]
        )

        assert [o["name"] for o in mine.json()] == ["reads.fastq"]
        assert theirs.status_code == 404

    async def test_another_profile_cannot_delete_a_project(self, client, two_profiles):
        project = await _project(two_profiles["a"].owner_id(), "a-undeletable")

        r = await client.delete(
            f"/api/v1/projects/{project.id}", headers=two_profiles["b_headers"]
        )

        assert r.status_code == 404
        assert (
            await project_service.get_project(
                project.id, owner=two_profiles["a"].owner_id()
            )
        ) is not None

    async def test_another_profile_gets_no_deletion_preview(self, client, two_profiles):
        project = await _project(two_profiles["a"].owner_id(), "a-preview")

        r = await client.get(
            f"/api/v1/projects/{project.id}/deletion-preview",
            headers=two_profiles["b_headers"],
        )

        assert r.status_code == 404


class TestRunsRouter:
    async def test_another_profile_cannot_fetch_a_run_by_id(self, client, two_profiles):
        """The one the brief calls out.

        `get_run` used to fetch the run unscoped and then ask for its status
        under the run's *own* owner, so any id resolved for anyone holding it.
        """
        run = await run_service.create_run(
            kind=RunKind.ALIGNMENT,
            project_id=PydanticObjectId(),
            label="a-run",
            inputs=[],
            params={},
            owner=two_profiles["a"].owner_id(),
        )

        mine = await client.get(f"/api/v1/runs/{run.id}", headers=two_profiles["a_headers"])
        theirs = await client.get(
            f"/api/v1/runs/{run.id}", headers=two_profiles["b_headers"]
        )

        assert mine.status_code == 200
        assert theirs.status_code == 404

    async def test_another_profiles_run_is_absent_from_the_listing(
        self, client, two_profiles
    ):
        await run_service.create_run(
            kind=RunKind.ALIGNMENT,
            project_id=PydanticObjectId(),
            label="a-listed-run",
            inputs=[],
            params={},
            owner=two_profiles["a"].owner_id(),
        )

        mine = await client.get("/api/v1/runs", headers=two_profiles["a_headers"])
        theirs = await client.get("/api/v1/runs", headers=two_profiles["b_headers"])

        assert "a-listed-run" in [r["label"] for r in mine.json()]
        assert "a-listed-run" not in [r["label"] for r in theirs.json()]

    async def test_another_profile_cannot_cancel_a_run(self, client, two_profiles):
        """Cancellation is destructive, so this is the worst of the run leaks:
        an unscoped fetch let one profile kill another's jobs by guessing."""
        run = await run_service.create_run(
            kind=RunKind.ALIGNMENT,
            project_id=PydanticObjectId(),
            label="a-cancellable",
            inputs=[],
            params={},
            owner=two_profiles["a"].owner_id(),
        )

        r = await client.post(
            f"/api/v1/runs/{run.id}/cancel", headers=two_profiles["b_headers"]
        )

        assert r.status_code == 404


class TestPipelinesRouter:
    async def test_another_profile_sees_no_references(self, client, two_profiles):
        """`list_references` reads objects straight out of a project, so it
        leaks names and sizes even though it never returns the object itself.

        Both directions are asserted, and here that is not belt-and-braces: this
        route answers 200 with a list rather than 404, so "B sees nothing" is
        also what a route hardcoded to `"local"` produces -- A's objects are not
        under `"local"` either. Only A's own request coming back *non-empty*
        distinguishes a scoped route from a broken one. Checked by mutation:
        pinning this call to `"local"` leaves the B assertion green and fails
        the A assertion.
        """
        project = await _project(two_profiles["a"].owner_id(), "a-references")
        await _object(
            two_profiles["a"].owner_id(),
            project.id,
            "genome.fasta",
            fmt_kind=FormatKind.FASTA,
            status=ObjectStatus.READY,
        )

        mine = await client.get(
            f"/api/v1/pipelines/references/{project.id}",
            headers=two_profiles["a_headers"],
        )
        theirs = await client.get(
            f"/api/v1/pipelines/references/{project.id}",
            headers=two_profiles["b_headers"],
        )

        assert [r["name"] for r in mine.json()["references"]] == ["genome.fasta"]
        assert theirs.json()["references"] == []


async def _job(owner: str, job_type: str, *, state: JobState = JobState.FAILED) -> Job:
    """A bare job row.

    Inserted rather than enqueued: `queue.enqueue` needs Redis, and what is
    under test is the route's owner filter, not dispatch. `state` defaults to
    FAILED because the retry route rejects anything non-terminal before the
    owner check would otherwise become observable.
    """
    job = Job(type=job_type, owner=owner, state=state)
    await job.insert()
    return job


class TestJobsRouter:
    async def test_another_profiles_job_is_absent_from_the_listing(
        self, client, two_profiles
    ):
        """Both directions, for the reason the pipelines test spells out.

        `list_jobs` answers 200 with a list, so "B sees nothing" is equally what
        a route hardcoded to `"local"` produces -- A's jobs are not under
        `"local"` either. Only A's own request coming back *containing its job*
        separates a scoped route from a broken one.
        """
        mine_job = await _job(two_profiles["a"].owner_id(), "a_listed_job")

        mine = await client.get("/api/v1/jobs", headers=two_profiles["a_headers"])
        theirs = await client.get("/api/v1/jobs", headers=two_profiles["b_headers"])

        assert str(mine_job.id) in [j["id"] for j in mine.json()]
        assert str(mine_job.id) not in [j["id"] for j in theirs.json()]

    async def test_another_profile_cannot_fetch_a_job_by_id(self, client, two_profiles):
        job = await _job(two_profiles["a"].owner_id(), "a_detail_job")

        mine = await client.get(f"/api/v1/jobs/{job.id}", headers=two_profiles["a_headers"])
        theirs = await client.get(
            f"/api/v1/jobs/{job.id}", headers=two_profiles["b_headers"]
        )

        assert mine.status_code == 200
        assert theirs.status_code == 404

    async def test_another_profile_cannot_cancel_a_job(self, client, two_profiles):
        """A write, and the worst of the job leaks: an unscoped cancel let one
        profile kill another's in-flight work from a guessed id."""
        job = await _job(
            two_profiles["a"].owner_id(), "a_cancellable", state=JobState.PENDING
        )

        r = await client.post(
            f"/api/v1/jobs/{job.id}/cancel", headers=two_profiles["b_headers"]
        )

        assert r.status_code == 404
        # The refusal has to be a no-op, not merely a 404 over a completed write.
        assert (await Job.get(job.id)).cancel_requested is False

    async def test_another_profile_cannot_retry_a_job(self, client, two_profiles):
        """Retry re-enqueues, so a leak here spends another profile's compute."""
        job = await _job(two_profiles["a"].owner_id(), "a_retryable")

        r = await client.post(
            f"/api/v1/jobs/{job.id}/retry", headers=two_profiles["b_headers"]
        )

        assert r.status_code == 404
        assert (await Job.get(job.id)).state is JobState.FAILED

    async def test_another_profile_cannot_read_a_jobs_log(self, client, two_profiles):
        """The log carries filenames and sample names from the tool's output."""
        job = await _job(two_profiles["a"].owner_id(), "a_logged_job")

        r = await client.get(
            f"/api/v1/jobs/{job.id}/log", headers=two_profiles["b_headers"]
        )

        assert r.status_code == 404

    async def test_jobs_require_a_profile_header(self, client, two_profiles):
        """The leak as measured against the running app: this returned 200 and a
        full page of every profile's jobs with no header at all."""
        r = await client.get("/api/v1/jobs")

        assert r.status_code == 400
        assert r.json()["code"] == "profile_unresolved"


async def _session(owner: str, project_name: str):
    """An open upload session, created through the service so its staging
    directory really exists -- the chunk and abort routes touch the filesystem,
    and a hand-built row would fail for a reason unrelated to the owner filter.
    """
    project = await _project(owner, project_name)
    session, _ = await upload_service.create_session(
        project_id=project.id,
        owner=owner,
        filename="reads.fastq",
        total_size=1024,
    )
    return session


class TestUploadsRouter:
    async def test_an_upload_is_filed_under_the_headers_owner(self, client, two_profiles):
        """The writer half of the partition for uploads.

        A session stamped "local" regardless of the header would leave every
        profile's uploads in one pile, and the isolation assertions elsewhere
        would still pass -- nobody would see anything.
        """
        project = await _project(two_profiles["b"].owner_id(), "b-uploads")

        r = await client.post(
            "/api/v1/uploads",
            json={
                "project_id": str(project.id),
                "filename": "reads.fastq",
                "total_size": 1024,
            },
            headers=two_profiles["b_headers"],
        )

        assert r.status_code == 201
        session_id = r.json()["session"]["id"]

        from app.models import UploadSession

        session = await UploadSession.get(PydanticObjectId(session_id))
        assert session.owner == two_profiles["b"].owner_id()
        assert session.owner != "local"

    async def test_another_profile_cannot_read_an_upload_session(
        self, client, two_profiles
    ):
        session = await _session(two_profiles["a"].owner_id(), "a-session-read")

        mine = await client.get(
            f"/api/v1/uploads/{session.id}", headers=two_profiles["a_headers"]
        )
        theirs = await client.get(
            f"/api/v1/uploads/{session.id}", headers=two_profiles["b_headers"]
        )

        assert mine.status_code == 200
        assert theirs.status_code == 404

    async def test_another_profiles_session_is_absent_from_the_listing(
        self, client, two_profiles
    ):
        """Both directions again -- this route answers 200 with a list."""
        session = await _session(two_profiles["a"].owner_id(), "a-session-listed")

        mine = await client.get("/api/v1/uploads", headers=two_profiles["a_headers"])
        theirs = await client.get("/api/v1/uploads", headers=two_profiles["b_headers"])

        assert str(session.id) in [s["id"] for s in mine.json()]
        assert str(session.id) not in [s["id"] for s in theirs.json()]

    async def test_another_profile_cannot_write_a_chunk(self, client, two_profiles):
        """The sharpest write in the file: an unscoped chunk PUT does not read
        someone else's upload, it writes bytes into the file they are
        assembling, surfacing later as a digest mismatch on their object.

        Both directions are asserted, and here that is load-bearing rather than
        belt-and-braces: B's 404 is also what a route hardcoded to `"local"`
        returns, because A's session is not under `"local"` either. Only A's own
        chunk write *succeeding* separates a scoped route from a broken one.
        Checked by mutation -- pinning this call to `"local"` leaves the B
        assertion green and fails the A assertion.
        """
        session = await _session(two_profiles["a"].owner_id(), "a-session-chunk")

        theirs = await client.put(
            f"/api/v1/uploads/{session.id}/chunks/0",
            content=b"bytes from another profile",
            headers=two_profiles["b_headers"],
        )

        assert theirs.status_code == 404
        assert (await UploadSession.get(session.id)).received_chunks == []

        mine = await client.put(
            f"/api/v1/uploads/{session.id}/chunks/0",
            content=b"bytes from the owner",
            headers=two_profiles["a_headers"],
        )

        assert mine.status_code == 200
        assert (await UploadSession.get(session.id)).received_chunks == [0]

    async def test_another_profile_cannot_abort_a_session(self, client, two_profiles):
        """Abort purges the staging directory, destroying transferred chunks.

        A's own abort is asserted too, for the same reason as the chunk test: a
        404 for B is what a `"local"` hardcode produces as well.
        """
        session = await _session(two_profiles["a"].owner_id(), "a-session-abort")

        theirs = await client.delete(
            f"/api/v1/uploads/{session.id}", headers=two_profiles["b_headers"]
        )

        assert theirs.status_code == 404
        assert (await UploadSession.get(session.id)).state is UploadState.OPEN

        mine = await client.delete(
            f"/api/v1/uploads/{session.id}", headers=two_profiles["a_headers"]
        )

        assert mine.status_code == 204
        assert (await UploadSession.get(session.id)).state is UploadState.ABORTED

    async def test_another_profile_cannot_complete_a_session(self, client, two_profiles):
        """Completing mints a DataObject under the *session's* owner, so an
        unscoped complete lets B force a half-transferred file into A's
        library.

        A's own attempt is asserted as *not* a 404 rather than as a success: the
        session has no chunks, so completing it legitimately fails -- but it
        fails as a 409 for missing chunks, having gotten past the owner lookup.
        A route hardcoded to `"local"` would 404 A as well, so the two status
        codes are what tells the wired route from the broken one.
        """
        session = await _session(two_profiles["a"].owner_id(), "a-session-complete")

        theirs = await client.post(
            f"/api/v1/uploads/{session.id}/complete", headers=two_profiles["b_headers"]
        )

        assert theirs.status_code == 404
        assert (await UploadSession.get(session.id)).state is UploadState.OPEN

        mine = await client.post(
            f"/api/v1/uploads/{session.id}/complete", headers=two_profiles["a_headers"]
        )

        assert mine.status_code == 409


class TestObjectServiceStillFiltersUnderneath:
    async def test_list_objects_is_scoped(self, two_profiles):
        """A guard on the layer the routes lean on.

        If this ever stops filtering, the route tests above go green for the
        wrong reason -- nothing would be visible to anyone.
        """
        project = await _project(two_profiles["a"].owner_id(), "a-service")
        await _object(two_profiles["a"].owner_id(), project.id, "reads.fastq")

        theirs = await object_service.list_objects(
            project.id, owner=two_profiles["b"].owner_id()
        )

        assert theirs == []
