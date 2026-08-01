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

from unittest.mock import AsyncMock

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
from app.services import (
    object_service,
    project_service,
    run_service,
    search_service,
    upload_service,
)

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
    digest: str | None = None,
) -> DataObject:
    """A bare object row.

    Inserted directly rather than through an ingest: what is under test is the
    route's owner filter, and going through the ingest path would drag a blob,
    a digest and a queue connection into a test about a query.

    `fmt_kind`/`status` exist for the reference listing, which filters on both
    before the owner filter is even observable -- an object left at the
    defaults would be absent from that response for the wrong reason.

    `digest` is the same kind of prerequisite for the launch tests: the launch
    services resolve an object's bytes before enqueueing, and one with no
    `blob_sha256` is refused for having no content long before the owner is
    consulted. The launch tests need the *positive* direction to actually
    reach the queue, so they pass one.
    """
    obj = DataObject(
        project_id=project_id,
        name=name,
        owner=owner,
        format=FormatInfo(kind=fmt_kind),
        status=status,
        blob_sha256=digest,
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

    async def test_another_profile_cannot_read_align_defaults(
        self, client, two_profiles
    ):
        """The read group in this response is built from the file's own sample
        name and platform, so serving it for a foreign object hands over the
        user's metadata, not just the server's constants."""
        project = await _project(two_profiles["a"].owner_id(), "a-defaults")
        obj = await _object(
            two_profiles["a"].owner_id(),
            project.id,
            "reads.fastq",
            fmt_kind=FormatKind.FASTQ,
            status=ObjectStatus.READY,
        )

        mine = await client.get(
            f"/api/v1/pipelines/align/defaults/{obj.id}",
            headers=two_profiles["a_headers"],
        )
        theirs = await client.get(
            f"/api/v1/pipelines/align/defaults/{obj.id}",
            headers=two_profiles["b_headers"],
        )

        assert mine.status_code == 200
        assert theirs.status_code == 404

    async def test_another_profile_cannot_read_variant_defaults(
        self, client, two_profiles
    ):
        """`reference_name` names a file in the caller's library, resolved by
        walking the BAM's provenance -- a foreign read would name another
        profile's genome."""
        project = await _project(two_profiles["a"].owner_id(), "a-variant-defaults")
        bam = await _object(
            two_profiles["a"].owner_id(),
            project.id,
            "aligned.bam",
            fmt_kind=FormatKind.BAM,
            status=ObjectStatus.READY,
        )

        mine = await client.get(
            f"/api/v1/pipelines/variants/defaults/{bam.id}",
            headers=two_profiles["a_headers"],
        )
        theirs = await client.get(
            f"/api/v1/pipelines/variants/defaults/{bam.id}",
            headers=two_profiles["b_headers"],
        )

        assert mine.status_code == 200
        assert theirs.status_code == 404

    async def test_another_profile_cannot_read_suggestions(
        self, client, two_profiles
    ):
        project = await _project(two_profiles["a"].owner_id(), "a-suggestions")
        obj = await _object(
            two_profiles["a"].owner_id(),
            project.id,
            "reads.fastq",
            fmt_kind=FormatKind.FASTQ,
            status=ObjectStatus.READY,
        )

        mine = await client.get(
            f"/api/v1/pipelines/suggestions/{obj.id}",
            headers=two_profiles["a_headers"],
        )
        theirs = await client.get(
            f"/api/v1/pipelines/suggestions/{obj.id}",
            headers=two_profiles["b_headers"],
        )

        assert mine.status_code == 200
        assert theirs.status_code == 404

    async def test_another_profile_cannot_read_computed_variants(
        self, client, two_profiles
    ):
        """The variant table is a SQLite file in a directory named by object
        id and nothing else, so this lookup is the only thing standing between
        one profile and another's called variants.

        B's 404 arrives before the path is built, which is why this passes even
        though neither profile has computed results here -- A gets the "compute
        results first" 404 from the missing database, B gets the ownership one.
        Both are 404s, so the bodies are what distinguish them.
        """
        project = await _project(two_profiles["a"].owner_id(), "a-variants")
        vcf = await _object(
            two_profiles["a"].owner_id(),
            project.id,
            "calls.vcf",
            fmt_kind=FormatKind.VCF,
            status=ObjectStatus.READY,
        )

        mine = await client.get(
            f"/api/v1/pipelines/vcfstats/variants/{vcf.id}",
            headers=two_profiles["a_headers"],
        )
        theirs = await client.get(
            f"/api/v1/pipelines/vcfstats/variants/{vcf.id}",
            headers=two_profiles["b_headers"],
        )

        assert mine.status_code == 404
        assert "Compute results first" in mine.text
        assert theirs.status_code == 404
        # B never reaches the message about computing results, because B never
        # got past the ownership check to learn whether any exist.
        assert "Compute results first" not in theirs.text

    async def test_another_profile_cannot_read_a_qc_report(
        self, client, two_profiles, tmp_path, monkeypatch
    ):
        """Report directories are named by object id, so without the ownership
        lookup the filesystem layout would be the access rule.

        A real report file is written so the two directions produce *different*
        statuses. Both profiles get a 404 otherwise -- A because the file is
        missing, B because of ownership -- and a test asserting only B's 404
        stays green against a route pinned to `"local"`. Established by
        mutation: that pinning survived the refusal-only version of this test.
        """
        project = await _project(two_profiles["a"].owner_id(), "a-qc-report")
        obj = await _object(
            two_profiles["a"].owner_id(),
            project.id,
            "reads.fastq",
            fmt_kind=FormatKind.FASTQ,
            status=ObjectStatus.READY,
        )

        from app.config import settings

        monkeypatch.setattr(settings, "bioinfo_home", tmp_path)
        report_dir = tmp_path / "qc_reports" / str(obj.id)
        report_dir.mkdir(parents=True)
        (report_dir / "fastp.html").write_text("<html>a's report</html>")

        mine = await client.get(
            f"/api/v1/pipelines/qc/report/{obj.id}/fastp.html",
            headers=two_profiles["a_headers"],
        )
        theirs = await client.get(
            f"/api/v1/pipelines/qc/report/{obj.id}/fastp.html",
            headers=two_profiles["b_headers"],
        )

        assert mine.status_code == 200
        assert "a's report" in mine.text
        assert theirs.status_code == 404

    async def test_pipelines_reads_require_a_profile_header(
        self, client, two_profiles
    ):
        """No header at all is refused, not silently defaulted to `"local"`."""
        project = await _project(two_profiles["a"].owner_id(), "a-no-header")
        obj = await _object(
            two_profiles["a"].owner_id(),
            project.id,
            "reads.fastq",
            fmt_kind=FormatKind.FASTQ,
            status=ObjectStatus.READY,
        )

        for path in (
            f"/api/v1/pipelines/references/{project.id}",
            f"/api/v1/pipelines/suggestions/{obj.id}",
            f"/api/v1/pipelines/align/defaults/{obj.id}",
            f"/api/v1/pipelines/variants/defaults/{obj.id}",
        ):
            resp = await client.get(path)
            assert resp.status_code == 400, path

    async def test_global_pipeline_reads_stay_open(self, client, two_profiles):
        """The deliberately unscoped routes, asserted as such.

        Tool availability and the trim defaults describe the container and the
        code, not anybody's library. They answer without a header on purpose --
        and a later pass that "finishes the sweep" by scoping them would break
        the launch dialog for a client that has not resolved a profile yet.
        """
        assert (await client.get("/api/v1/pipelines/tools")).status_code == 200
        assert (await client.get("/api/v1/pipelines/defaults")).status_code == 200
        assert (
            await client.get("/api/v1/pipelines/summary/status")
        ).status_code == 200
        assert (
            await client.get("/api/v1/pipelines/aligners/minimap2/schema")
        ).status_code == 200


class TestPipelineLaunchesAreScoped:
    """Launch routes, which are the ones worth the most care.

    A read route with the wrong owner shows someone a filename. A *launch*
    route with the wrong owner spends the machine's CPU on another profile's
    file and writes the output into a library, where it persists after the
    request is forgotten and after the mistake is noticed. That asymmetry is
    why these are tested harder than the reads above.

    **Every case asserts both directions, and the positive one is the load-
    bearing half.** A refusal on its own proves nothing here: a route pinned
    to a hardcoded `"local"` also refuses B, because A's object is not under
    `"local"` either -- so a B-only assertion stays green against exactly the
    bug it is meant to catch. This was confirmed by mutation rather than
    reasoned about: pinning `launch_qc` to `"local"` left a B-only test
    passing. What kills that mutation is asserting A's own launch reaches the
    queue *carrying A's owner*, which is what `_assert_scoped_launch` does.

    `queue.enqueue` is mocked throughout: what is under test is the route's
    scoping, and a real enqueue would need Redis. The mock does double duty --
    `assert_not_awaited` proves B was refused before any work was queued, and
    the recorded `owner` kwarg proves A's job was filed under A.
    """

    async def _assert_scoped_launch(
        self, client, two_profiles, monkeypatch, *, path: str, body: dict
    ):
        """Post `body` to `path` as B, then as A, and assert both directions.

        Returns nothing; the assertions are the point. B must be refused with a
        404 and reach no queue, and A must reach the queue with A's own owner
        string -- not `"local"`, and not the reference's or the mate's.
        """
        from app.queue import queue

        # Returns a real Job rather than a bare AsyncMock: the launch routes
        # serialize whatever comes back through `JobOut.of`, which needs actual
        # fields. A mock's auto-attributes fail that validation, and the 500
        # that follows would mask the very success this test is asserting.
        enqueue = AsyncMock(return_value=Job(type="probe", owner="unused"))
        monkeypatch.setattr(queue, "enqueue", enqueue)

        refused = await client.post(
            path, json=body, headers=two_profiles["b_headers"]
        )
        assert refused.status_code == 404, refused.text
        enqueue.assert_not_awaited()

        allowed = await client.post(
            path, json=body, headers=two_profiles["a_headers"]
        )
        assert allowed.status_code < 400, allowed.text
        assert enqueue.await_count >= 1
        assert (
            enqueue.await_args_list[0].kwargs["owner"]
            == two_profiles["a"].owner_id()
        )

    async def _reads(self, two_profiles, name: str = "reads.fastq"):
        project = await _project(two_profiles["a"].owner_id(), f"a-launch-{name}")
        obj = await _object(
            two_profiles["a"].owner_id(),
            project.id,
            name,
            fmt_kind=FormatKind.FASTQ,
            status=ObjectStatus.READY,
            digest="a" * 64,
        )
        return project, obj

    async def test_qc_is_scoped(self, client, two_profiles, monkeypatch):
        _, obj = await self._reads(two_profiles, "qc.fastq")
        await self._assert_scoped_launch(
            client,
            two_profiles,
            monkeypatch,
            path="/api/v1/pipelines/qc",
            body={"object_id": str(obj.id)},
        )

    async def test_trim_is_scoped(self, client, two_profiles, monkeypatch):
        _, obj = await self._reads(two_profiles, "trim.fastq")
        await self._assert_scoped_launch(
            client,
            two_profiles,
            monkeypatch,
            path="/api/v1/pipelines/trim",
            body={"object_id": str(obj.id), "paired": False},
        )

    async def test_bam_stats_is_scoped(self, client, two_profiles, monkeypatch):
        project = await _project(two_profiles["a"].owner_id(), "a-bamstats")
        bam = await _object(
            two_profiles["a"].owner_id(),
            project.id,
            "aligned.bam",
            fmt_kind=FormatKind.BAM,
            status=ObjectStatus.READY,
            digest="b" * 64,
        )
        bam.facts = {"sorted": True, "sort_order": "coordinate"}
        await bam.save()
        await self._assert_scoped_launch(
            client,
            two_profiles,
            monkeypatch,
            path="/api/v1/pipelines/bamstats",
            body={"object_id": str(bam.id)},
        )

    async def test_vcf_stats_is_scoped(self, client, two_profiles, monkeypatch):
        project = await _project(two_profiles["a"].owner_id(), "a-vcfstats")
        vcf = await _object(
            two_profiles["a"].owner_id(),
            project.id,
            "calls.vcf",
            fmt_kind=FormatKind.VCF,
            status=ObjectStatus.READY,
            digest="c" * 64,
        )
        await self._assert_scoped_launch(
            client,
            two_profiles,
            monkeypatch,
            path="/api/v1/pipelines/vcfstats",
            body={"object_id": str(vcf.id)},
        )

    async def test_index_build_is_scoped(self, client, two_profiles, monkeypatch):
        project = await _project(two_profiles["a"].owner_id(), "a-buildindex")
        ref = await _object(
            two_profiles["a"].owner_id(),
            project.id,
            "genome.fasta",
            fmt_kind=FormatKind.FASTA,
            status=ObjectStatus.READY,
            digest="d" * 64,
        )
        await self._assert_scoped_launch(
            client,
            two_profiles,
            monkeypatch,
            path="/api/v1/pipelines/index",
            body={"reference_id": str(ref.id)},
        )

    async def test_summary_refuses_a_foreign_object_without_a_409(
        self, client, two_profiles, monkeypatch
    ):
        """The summary service answers "nothing to do" with None, which the
        route renders as a 409. A foreign id must not land there.

        Asserted on its own rather than through `_assert_scoped_launch`,
        because the positive direction depends on `llm_summaries_enabled` and
        on the file having facts to summarize -- neither of which this test
        controls. What it does pin down is that B's refusal is a 404 and not
        the 409, since a 409 would confirm to B that the object exists.
        """
        from app.queue import queue

        _, obj = await self._reads(two_profiles, "summary.fastq")
        enqueue = AsyncMock()
        monkeypatch.setattr(queue, "enqueue", enqueue)

        resp = await client.post(
            "/api/v1/pipelines/summary",
            json={"object_id": str(obj.id)},
            headers=two_profiles["b_headers"],
        )

        assert resp.status_code == 404
        enqueue.assert_not_awaited()

    async def test_annotation_is_scoped(self, client, two_profiles, monkeypatch):
        """Same shape as variant calling: A cannot reach the queue either --
        annotation needs a resolvable reference and its `.fai` -- so the
        distinguishing assertion is again that A's failure is *not* a
        not-found. Pinning the service lookup to `"local"` turns A's answer
        into one and fails this."""
        from app.queue import queue

        project = await _project(two_profiles["a"].owner_id(), "a-annot")
        vcf = await _object(
            two_profiles["a"].owner_id(),
            project.id,
            "calls.vcf",
            fmt_kind=FormatKind.VCF,
            status=ObjectStatus.READY,
            digest="e" * 64,
        )
        enqueue = AsyncMock()
        monkeypatch.setattr(queue, "enqueue", enqueue)

        refused = await client.post(
            "/api/v1/pipelines/annotate",
            json={"object_id": str(vcf.id)},
            headers=two_profiles["b_headers"],
        )
        assert refused.status_code == 404
        assert refused.json()["code"] == "not_found"
        enqueue.assert_not_awaited()

        mine = await client.post(
            "/api/v1/pipelines/annotate",
            json={"object_id": str(vcf.id)},
            headers=two_profiles["a_headers"],
        )
        assert mine.json()["code"] != "not_found", mine.text
        enqueue.assert_not_awaited()

    async def test_variant_calling_is_scoped(
        self, client, two_profiles, monkeypatch
    ):
        """B is refused, and A gets *past* the ownership check.

        This one cannot assert on an enqueued owner: `launch_variant_calling`
        deliberately refuses to build the `.bai` and reference `.fai` it needs,
        so A never reaches the queue in this fixture either. The distinguishing
        assertion is therefore the *shape* of A's failure -- A is stopped by a
        missing index, B by ownership, and those are different errors.

        Without that second assertion this test is worthless, which was
        established by mutation rather than assumed: pinning the service's BAM
        lookup to `"local"` left a B-only version green, because A's BAM is not
        under `"local"` either. Under that mutation A gets a 404 "Object not
        found" instead of the 400 about a missing index, and this test fails.
        """
        from app.queue import queue

        project = await _project(two_profiles["a"].owner_id(), "a-calling")
        bam = await _object(
            two_profiles["a"].owner_id(),
            project.id,
            "aligned.bam",
            fmt_kind=FormatKind.BAM,
            status=ObjectStatus.READY,
            digest="f" * 64,
        )
        enqueue = AsyncMock()
        monkeypatch.setattr(queue, "enqueue", enqueue)

        refused = await client.post(
            "/api/v1/pipelines/variants",
            json={"bam_id": str(bam.id)},
            headers=two_profiles["b_headers"],
        )
        assert refused.status_code == 404
        assert refused.json()["code"] == "not_found"
        enqueue.assert_not_awaited()

        # A resolves its own BAM and is stopped later, by the missing index --
        # a different failure from B's, which is the whole point.
        mine = await client.post(
            "/api/v1/pipelines/variants",
            json={"bam_id": str(bam.id)},
            headers=two_profiles["a_headers"],
        )
        assert mine.json()["code"] != "not_found", mine.text
        enqueue.assert_not_awaited()

    async def test_alignment_is_scoped(self, client, two_profiles, monkeypatch):
        """Both inputs belong to A, so B is refused on the reads -- and A's own
        launch must still reach the queue under A.

        The positive half is not decoration. Checked by mutation: pinning this
        route's `owner=` to `"local"` leaves the refusal green, because A's
        reads are not under `"local"` either. Only the enqueued owner tells the
        two apart.

        An unindexed reference makes `launch_alignment` queue `build_index`
        first and the alignment behind it, so the first enqueue here is the
        index build -- which is exactly the call whose owner decides whose
        library the resulting `.mmi` lands in.
        """
        project = await _project(two_profiles["a"].owner_id(), "a-align")
        reads = await _object(
            two_profiles["a"].owner_id(),
            project.id,
            "reads.fastq",
            fmt_kind=FormatKind.FASTQ,
            status=ObjectStatus.READY,
            digest="1" * 64,
        )
        ref = await _object(
            two_profiles["a"].owner_id(),
            project.id,
            "genome.fasta",
            fmt_kind=FormatKind.FASTA,
            status=ObjectStatus.READY,
            digest="2" * 64,
        )

        await self._assert_scoped_launch(
            client,
            two_profiles,
            monkeypatch,
            path="/api/v1/pipelines/align",
            body={
                "object_id": str(reads.id),
                "reference_id": str(ref.id),
                "paired": False,
            },
        )

    async def test_a_profile_cannot_align_its_own_reads_to_a_foreign_reference(
        self, client, two_profiles, monkeypatch
    ):
        """The mixed case, which the single-owner tests cannot reach.

        B owns the reads and asks to align them against A's genome. The reads
        resolve under B, so the refusal has to come from the *reference's* own
        scoped lookup -- this is the assertion that fails if `object_id` were
        scoped and `reference_id` were left reading whatever it was handed.
        """
        from app.queue import queue

        a_project = await _project(two_profiles["a"].owner_id(), "a-genome")
        foreign_ref = await _object(
            two_profiles["a"].owner_id(),
            a_project.id,
            "genome.fasta",
            fmt_kind=FormatKind.FASTA,
            status=ObjectStatus.READY,
            digest="3" * 64,
        )
        b_project = await _project(two_profiles["b"].owner_id(), "b-reads")
        my_reads = await _object(
            two_profiles["b"].owner_id(),
            b_project.id,
            "reads.fastq",
            fmt_kind=FormatKind.FASTQ,
            status=ObjectStatus.READY,
            digest="4" * 64,
        )
        enqueue = AsyncMock()
        monkeypatch.setattr(queue, "enqueue", enqueue)

        resp = await client.post(
            "/api/v1/pipelines/align",
            json={
                "object_id": str(my_reads.id),
                "reference_id": str(foreign_ref.id),
                "paired": False,
            },
            headers=two_profiles["b_headers"],
        )

        assert resp.status_code == 404
        enqueue.assert_not_awaited()

    async def test_a_profile_cannot_trim_against_a_foreign_mate(
        self, client, two_profiles, monkeypatch
    ):
        """The mate is caller-supplied too, so it gets its own scoped lookup.

        Same shape as the alignment case above: B owns the R1, so the refusal
        can only be coming from the explicit `mate_object_id` being resolved
        under B's scope.
        """
        from app.queue import queue

        a_project = await _project(two_profiles["a"].owner_id(), "a-mate")
        foreign_mate = await _object(
            two_profiles["a"].owner_id(),
            a_project.id,
            "sample_R2.fastq",
            fmt_kind=FormatKind.FASTQ,
            status=ObjectStatus.READY,
            digest="5" * 64,
        )
        b_project = await _project(two_profiles["b"].owner_id(), "b-mate")
        my_reads = await _object(
            two_profiles["b"].owner_id(),
            b_project.id,
            "sample_R1.fastq",
            fmt_kind=FormatKind.FASTQ,
            status=ObjectStatus.READY,
            digest="6" * 64,
        )
        enqueue = AsyncMock()
        monkeypatch.setattr(queue, "enqueue", enqueue)

        resp = await client.post(
            "/api/v1/pipelines/trim",
            json={
                "object_id": str(my_reads.id),
                "mate_object_id": str(foreign_mate.id),
                "paired": True,
            },
            headers=two_profiles["b_headers"],
        )

        assert resp.status_code == 404
        enqueue.assert_not_awaited()

    async def test_launches_require_a_profile_header(self, client, two_profiles):
        """No header is refused outright rather than defaulted."""
        _, obj = await self._reads(two_profiles, "noheader.fastq")

        resp = await client.post(
            "/api/v1/pipelines/qc", json={"object_id": str(obj.id)}
        )
        assert resp.status_code == 400


class TestNcbiRouter:
    """The download routes, and the dedup checks in front of them.

    A dedup check that spanned profiles would tell one profile that another
    had already fetched an accession -- greying out a download the caller does
    not actually hold. That is a silent wrong answer rather than an error,
    which is why it is asserted in both directions here.
    """

    async def test_the_sra_download_is_scoped(
        self, client, two_profiles, monkeypatch
    ):
        """B cannot download into A's project, and A still can.

        The positive half is required, not decorative: pinning the project
        lookup to `"local"` leaves a B-only assertion green, because A's
        project is not owned by `"local"` either. Confirmed by mutation.

        `_resolve_metadata` is stubbed because the real one calls NCBI over the
        network; what is under test is which profile the job is filed under,
        not what SRA says about SRR1.
        """
        from app.queue import queue
        from app.services import sra_service

        project = await _project(two_profiles["a"].owner_id(), "a-sra")

        async def _meta(accessions):
            return {a: {"platform": "ILLUMINA", "metadata": {}} for a in accessions}

        monkeypatch.setattr(sra_service, "_resolve_metadata", _meta)
        enqueue = AsyncMock(return_value=Job(type="probe", owner="unused"))
        monkeypatch.setattr(queue, "enqueue", enqueue)

        body = {"project_id": str(project.id), "run_accessions": ["SRR1"]}

        # `/sra/download`, not `/ncbi/download`: the SRA download never moved
        # when the resolver did, and only the `/sra/*` alias mounts it. An
        # earlier draft of this test posted to `/ncbi/download` and "passed" on
        # Starlette's routing 404 without ever reaching the ownership check --
        # which is exactly the class of empty test this module exists to avoid,
        # and is why the positive direction below is not optional.
        refused = await client.post(
            "/api/v1/sra/download", json=body, headers=two_profiles["b_headers"]
        )
        assert refused.status_code == 404
        assert refused.json()["code"] == "not_found"
        enqueue.assert_not_awaited()

        allowed = await client.post(
            "/api/v1/sra/download", json=body, headers=two_profiles["a_headers"]
        )
        assert allowed.status_code < 400, allowed.text
        assert enqueue.await_count >= 1
        assert (
            enqueue.await_args_list[0].kwargs["owner"]
            == two_profiles["a"].owner_id()
        )

    async def test_the_assembly_download_is_scoped(
        self, client, two_profiles, monkeypatch
    ):
        """B cannot fetch a genome into A's project, and A still can.

        Both directions, for the reason established by mutation on the SRA
        twin: pinning the project lookup to `"local"` leaves a refusal-only
        test green, since A's project is not `"local"`-owned either.

        `assembly.lookup` and `component_availability` are stubbed -- the real
        pair makes a blocking HTTP call and shells out to the `datasets` CLI,
        neither of which this test is about.
        """
        from app.metadata import assembly
        from app.queue import queue

        project = await _project(two_profiles["a"].owner_id(), "a-assembly")
        monkeypatch.setattr(
            assembly,
            "lookup",
            lambda a: assembly.AssemblyMetadata(accession=a, organism="E. coli"),
        )
        monkeypatch.setattr(assembly, "component_availability", lambda a: [])
        enqueue = AsyncMock(return_value=Job(type="probe", owner="unused"))
        monkeypatch.setattr(queue, "enqueue", enqueue)

        body = {
            "project_id": str(project.id),
            "accession": "GCF_000002445.2",
            "components": ["genome"],
        }

        refused = await client.post(
            "/api/v1/ncbi/download-assembly",
            json=body,
            headers=two_profiles["b_headers"],
        )
        assert refused.status_code == 404
        # The body, not just the status: Starlette answers an unrouted path
        # with a 404 too, and a test that accepted that would pass without ever
        # reaching the ownership check. This is the app's own error shape.
        assert refused.json()["code"] == "not_found"
        enqueue.assert_not_awaited()

        allowed = await client.post(
            "/api/v1/ncbi/download-assembly",
            json=body,
            headers=two_profiles["a_headers"],
        )
        assert allowed.status_code < 400, allowed.text
        assert enqueue.await_count >= 1
        assert (
            enqueue.await_args_list[0].kwargs["owner"]
            == two_profiles["a"].owner_id()
        )

    async def test_the_sra_dedup_check_does_not_span_profiles(self, two_profiles):
        """A's downloaded run must not read as B's.

        Both directions, and here that matters for the reason CLAUDE.md warns
        about: "B sees nothing" alone is also what a check hardcoded to
        `"local"` returns, since A's row is not under `"local"` either. Only
        A's own lookup coming back *non-empty* separates a scoped query from a
        broken one.
        """
        from app.services import sra_service

        project = await _project(two_profiles["a"].owner_id(), "a-dedup")
        obj = await _object(
            two_profiles["a"].owner_id(),
            project.id,
            "SRR999.fastq",
            fmt_kind=FormatKind.FASTQ,
            status=ObjectStatus.READY,
        )
        obj.metadata = {"sra_run": "SRR999"}
        await obj.save()

        mine = await sra_service.already_downloaded(
            project.id, ["SRR999"], owner=two_profiles["a"].owner_id()
        )
        theirs = await sra_service.already_downloaded(
            project.id, ["SRR999"], owner=two_profiles["b"].owner_id()
        )

        assert mine == {"SRR999"}
        assert theirs == set()

    async def test_the_assembly_dedup_check_does_not_span_profiles(
        self, two_profiles
    ):
        """Same both-directions reasoning as the SRA dedup check above."""
        from app.models import ObjectRole
        from app.services import assembly_service

        project = await _project(two_profiles["a"].owner_id(), "a-asm-dedup")
        obj = await _object(
            two_profiles["a"].owner_id(),
            project.id,
            "genome.fasta",
            fmt_kind=FormatKind.FASTA,
            status=ObjectStatus.READY,
        )
        obj.role = ObjectRole.REFERENCE
        obj.metadata = {"assembly_accession": "GCF_000002445.2"}
        await obj.save()

        mine = await assembly_service.already_downloaded(
            project.id, "GCF_000002445.2", owner=two_profiles["a"].owner_id()
        )
        theirs = await assembly_service.already_downloaded(
            project.id, "GCF_000002445.2", owner=two_profiles["b"].owner_id()
        )

        assert mine is True
        assert theirs is False

    async def test_ncbi_routes_require_a_profile_header(self, client, two_profiles):
        resp = await client.post(
            "/api/v1/ncbi/resolve", json={"accession": "SRR1"}
        )
        assert resp.status_code == 400


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


class TestUniProtRouter:
    async def test_the_uniprot_download_is_scoped(
        self, client, two_profiles, monkeypatch
    ):
        """B cannot fetch a proteome into A's project, and A still can.

        Both directions, for the reason the assembly twin establishes: pinning
        the project lookup to `"local"` leaves a refusal-only test green, since
        A's project is not `"local"`-owned either.

        Nothing here touches UniProt. The route's own work before the enqueue
        is a project lookup and some string building; the network calls live in
        the resolve endpoint rather than this one.
        """
        from app.queue import queue

        project = await _project(two_profiles["a"].owner_id(), "a-uniprot")
        enqueue = AsyncMock(return_value=Job(type="probe", owner="unused"))
        monkeypatch.setattr(queue, "enqueue", enqueue)

        body = {
            "project_id": str(project.id),
            "proteome_id": "UP000002311",
            "accessions": [],
            "reviewed_only": True,
        }

        refused = await client.post(
            "/api/v1/uniprot/download",
            json=body,
            headers=two_profiles["b_headers"],
        )
        assert refused.status_code == 404
        # The body, not just the status: an unrouted path answers 404 too, and
        # a test that accepted that would pass without reaching the check.
        assert refused.json()["code"] == "not_found"
        enqueue.assert_not_awaited()

        allowed = await client.post(
            "/api/v1/uniprot/download",
            json=body,
            headers=two_profiles["a_headers"],
        )
        assert allowed.status_code < 400, allowed.text
        assert enqueue.await_count >= 1
        assert (
            enqueue.await_args_list[0].kwargs["owner"]
            == two_profiles["a"].owner_id()
        )


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


class TestSearchRouter:
    """The widest read surface in the API, and the one write that could be
    applied to rows the caller never sees.

    Every read assertion here checks *both* directions, and that is
    load-bearing rather than thorough: these routes answer 200 with a list, so
    "B sees nothing" is also exactly what a route hardcoded to `"local"`
    produces -- A's objects are not filed under `"local"` either. Only A's own
    request coming back non-empty separates a scoped route from a broken one.
    Verified by mutation; see the module docstring.
    """

    async def test_search_does_not_cross_profiles(self, client, two_profiles):
        project = await _project(two_profiles["a"].owner_id(), "a-search")
        await _object(two_profiles["a"].owner_id(), project.id, "a-secret-sample.fastq")

        mine = await client.get(
            "/api/v1/search/objects?q=a-secret-sample",
            headers=two_profiles["a_headers"],
        )
        theirs = await client.get(
            "/api/v1/search/objects?q=a-secret-sample",
            headers=two_profiles["b_headers"],
        )

        assert [o["name"] for o in mine.json()["objects"]] == ["a-secret-sample.fastq"]
        assert theirs.json()["objects"] == []

    async def test_the_total_is_scoped_too(self, client, two_profiles):
        """`total` is computed from a second, separately built filter.

        It is the count the UI renders as "N results", so an unscoped total
        discloses how many files another profile holds even when the page of
        objects itself comes back correctly filtered.
        """
        project = await _project(two_profiles["a"].owner_id(), "a-total")
        await _object(two_profiles["a"].owner_id(), project.id, "a-counted.fastq")

        mine = await client.get(
            "/api/v1/search/objects?q=a-counted", headers=two_profiles["a_headers"]
        )
        theirs = await client.get(
            "/api/v1/search/objects?q=a-counted", headers=two_profiles["b_headers"]
        )

        assert mine.json()["total"] == 1
        assert theirs.json()["total"] == 0

    async def test_facets_do_not_expose_another_profiles_tags(
        self, client, two_profiles
    ):
        """Facets leak by counting: no document is returned, but a tag list
        naming another profile's cohorts describes a library the caller cannot
        open."""
        project = await _project(two_profiles["a"].owner_id(), "a-facets")
        obj = await _object(two_profiles["a"].owner_id(), project.id, "tagged.fastq")
        await obj.set({"tags": ["a-private-cohort"]})

        mine = await client.get("/api/v1/search/facets", headers=two_profiles["a_headers"])
        theirs = await client.get(
            "/api/v1/search/facets", headers=two_profiles["b_headers"]
        )

        assert "a-private-cohort" in [t["value"] for t in mine.json()["tags"]]
        assert "a-private-cohort" not in [t["value"] for t in theirs.json()["tags"]]

    async def test_facets_do_not_expose_another_profiles_metadata_keys(
        self, client, two_profiles
    ):
        project = await _project(two_profiles["a"].owner_id(), "a-facet-keys")
        obj = await _object(two_profiles["a"].owner_id(), project.id, "keyed.fastq")
        await obj.set({"metadata": {"a_private_key": "x"}})

        mine = await client.get("/api/v1/search/facets", headers=two_profiles["a_headers"])
        theirs = await client.get(
            "/api/v1/search/facets", headers=two_profiles["b_headers"]
        )

        assert "a_private_key" in [k["key"] for k in mine.json()["metadata_keys"]]
        assert "a_private_key" not in [k["key"] for k in theirs.json()["metadata_keys"]]

    async def test_metadata_values_do_not_cross_profiles(self, client, two_profiles):
        """The most directly disclosing read of the three: the values are the
        data. An unscoped `sample_id` picker hands over every patient
        identifier in the database."""
        project = await _project(two_profiles["a"].owner_id(), "a-values")
        obj = await _object(two_profiles["a"].owner_id(), project.id, "patient.fastq")
        await obj.set({"metadata": {"sample_id": "P-041-PRIVATE"}})

        mine = await client.get(
            "/api/v1/search/metadata-values/sample_id",
            headers=two_profiles["a_headers"],
        )
        theirs = await client.get(
            "/api/v1/search/metadata-values/sample_id",
            headers=two_profiles["b_headers"],
        )

        assert "P-041-PRIVATE" in [v["value"] for v in mine.json()["values"]]
        assert "P-041-PRIVATE" not in [v["value"] for v in theirs.json()["values"]]

    async def test_search_requires_a_profile_header(self, client, two_profiles):
        """The leak as measured against the running app: this answered 200 with
        every profile's filenames and no header at all."""
        r = await client.get("/api/v1/search/objects?q=")

        assert r.status_code == 400
        assert r.json()["code"] == "profile_unresolved"

    async def test_metadata_schemas_stay_open(self, client, two_profiles):
        """The two routes in this file that are deliberately *not* scoped.

        They render the FormatKind/ObjectRole enums and read no collection, so
        the answer is a property of the code and identical for every profile.
        Asserted so that a later sweep adding `OwnerDep` everywhere has to
        justify breaking the field pickers rather than doing it by reflex.
        """
        r = await client.get("/api/v1/metadata/schemas")

        assert r.status_code == 200
        assert "common" in r.json()


class TestSearchBulkWrites:
    """The worst of the search leaks, and worse in kind than the reads above.

    An unscoped search discloses; an unscoped bulk write *modifies*. These two
    routes take raw object ids and previously applied `$set`/`$addToSet`/`$pull`
    to whatever they matched, so one profile could rewrite another's metadata
    and tags from guessed ids -- and unlike a disclosure, the owner has no way
    to notice or undo it.

    A batch mixing the caller's ids with someone else's is refused whole rather
    than partially applied, so each test below asserts the refusal *and*
    re-reads A's row to prove the write was a no-op rather than a 404 over a
    completed update.
    """

    async def test_another_profile_cannot_tag_a_row(self, client, two_profiles):
        project = await _project(two_profiles["a"].owner_id(), "a-bulk-tags")
        obj = await _object(two_profiles["a"].owner_id(), project.id, "reads.fastq")

        r = await client.post(
            "/api/v1/objects/bulk-tags",
            json={"object_ids": [str(obj.id)], "add": ["b-was-here"]},
            headers=two_profiles["b_headers"],
        )

        assert r.status_code == 404
        assert (await DataObject.get(obj.id)).tags == []

    async def test_another_profile_cannot_untag_a_row(self, client, two_profiles):
        """The `$pull` half, which the `$addToSet` test does not reach: tag
        removal is a second update_many with its own filter."""
        project = await _project(two_profiles["a"].owner_id(), "a-bulk-untag")
        obj = await _object(two_profiles["a"].owner_id(), project.id, "reads.fastq")
        await obj.set({"tags": ["keep-me"]})

        r = await client.post(
            "/api/v1/objects/bulk-tags",
            json={"object_ids": [str(obj.id)], "remove": ["keep-me"]},
            headers=two_profiles["b_headers"],
        )

        assert r.status_code == 404
        assert (await DataObject.get(obj.id)).tags == ["keep-me"]

    async def test_another_profile_cannot_set_metadata(self, client, two_profiles):
        project = await _project(two_profiles["a"].owner_id(), "a-bulk-meta")
        obj = await _object(two_profiles["a"].owner_id(), project.id, "reads.fastq")
        await obj.set({"metadata": {"sample_id": "P-041"}})

        r = await client.post(
            "/api/v1/objects/bulk-metadata",
            json={"object_ids": [str(obj.id)], "set": {"sample_id": "OVERWRITTEN"}},
            headers=two_profiles["b_headers"],
        )

        assert r.status_code == 404
        assert (await DataObject.get(obj.id)).metadata["sample_id"] == "P-041"

    async def test_another_profile_cannot_unset_metadata(self, client, two_profiles):
        """`$unset` deletes rather than overwrites, so a leak here destroys
        data outright instead of replacing it with something recoverable."""
        project = await _project(two_profiles["a"].owner_id(), "a-bulk-unset")
        obj = await _object(two_profiles["a"].owner_id(), project.id, "reads.fastq")
        await obj.set({"metadata": {"sample_id": "P-041"}})

        r = await client.post(
            "/api/v1/objects/bulk-metadata",
            json={"object_ids": [str(obj.id)], "unset": ["sample_id"]},
            headers=two_profiles["b_headers"],
        )

        assert r.status_code == 404
        assert (await DataObject.get(obj.id)).metadata["sample_id"] == "P-041"

    async def test_a_mixed_batch_is_refused_whole(self, client, two_profiles):
        """The case the all-or-nothing choice exists for.

        B sends one of its own ids and one of A's. The alternative design --
        filter the update and report a short count -- would apply the edit to
        B's row and answer `matched: 1`, which B cannot distinguish from "the
        other id was deleted". Neither row is touched here.
        """
        a_project = await _project(two_profiles["a"].owner_id(), "a-mixed")
        a_obj = await _object(two_profiles["a"].owner_id(), a_project.id, "a.fastq")
        b_project = await _project(two_profiles["b"].owner_id(), "b-mixed")
        b_obj = await _object(two_profiles["b"].owner_id(), b_project.id, "b.fastq")

        r = await client.post(
            "/api/v1/objects/bulk-tags",
            json={
                "object_ids": [str(b_obj.id), str(a_obj.id)],
                "add": ["batch-tag"],
            },
            headers=two_profiles["b_headers"],
        )

        assert r.status_code == 404
        assert (await DataObject.get(a_obj.id)).tags == []
        # B's own row is not a consolation prize: the batch was refused, so its
        # own id must be untouched too, or the refusal was really a partial
        # write with an error code on top.
        assert (await DataObject.get(b_obj.id)).tags == []

    async def test_the_refusal_does_not_confirm_the_row_exists(
        self, client, two_profiles
    ):
        """A wrong-owner id and a nonexistent id must answer identically.

        Otherwise the error itself becomes the oracle the partition was meant
        to remove: B could probe ids and learn which ones are real.
        """
        a_project = await _project(two_profiles["a"].owner_id(), "a-probe")
        a_obj = await _object(two_profiles["a"].owner_id(), a_project.id, "a.fastq")

        real_but_theirs = await client.post(
            "/api/v1/objects/bulk-tags",
            json={"object_ids": [str(a_obj.id)], "add": ["x"]},
            headers=two_profiles["b_headers"],
        )
        never_existed = await client.post(
            "/api/v1/objects/bulk-tags",
            json={"object_ids": [str(PydanticObjectId())], "add": ["x"]},
            headers=two_profiles["b_headers"],
        )

        assert real_but_theirs.status_code == never_existed.status_code == 404
        assert real_but_theirs.json()["code"] == never_existed.json()["code"]
        assert real_but_theirs.json()["message"] == never_existed.json()["message"]

    async def test_a_profile_can_still_bulk_edit_its_own_rows(
        self, client, two_profiles
    ):
        """The direction that proves the refusals above are not just a route
        that rejects everything."""
        project = await _project(two_profiles["b"].owner_id(), "b-own-bulk")
        obj = await _object(two_profiles["b"].owner_id(), project.id, "mine.fastq")

        r = await client.post(
            "/api/v1/objects/bulk-tags",
            json={"object_ids": [str(obj.id)], "add": ["mine"]},
            headers=two_profiles["b_headers"],
        )

        assert r.status_code == 200
        assert r.json()["modified"] == 1
        assert (await DataObject.get(obj.id)).tags == ["mine"]

    async def test_bulk_writes_require_a_profile_header(self, client, two_profiles):
        project = await _project(two_profiles["a"].owner_id(), "a-headerless-bulk")
        obj = await _object(two_profiles["a"].owner_id(), project.id, "reads.fastq")

        r = await client.post(
            "/api/v1/objects/bulk-tags",
            json={"object_ids": [str(obj.id)], "add": ["x"]},
        )

        assert r.status_code == 400
        assert r.json()["code"] == "profile_unresolved"
        assert (await DataObject.get(obj.id)).tags == []


class TestBulkWriteFilterIsIndependentlyLoadBearing:
    """The write filters, proven without the pre-check standing in front of them.

    Every test in `TestSearchBulkWrites` goes through `_assert_all_owned`, which
    refuses a foreign id before the `update_many` is ever issued. That makes
    those tests blind to the owner clause *on the update itself*: stripping
    `"owner": owner` from both `update_many` filters leaves all eight of them
    green, because the refusal happens first. Confirmed by mutation -- that is
    why this class exists, and it must not be folded into the one above.

    The two are not redundant. The pre-check and the write are separate round
    trips, so the filter is what actually holds at the moment of writing; the
    pre-check only converts a partial write into a clean refusal. Each test
    here calls the service directly with the check satisfied -- an id the
    caller *does* own -- alongside a foreign id smuggled past it, which is the
    only way to observe the filter alone.
    """

    async def _pair(self, two_profiles, label):
        """One row owned by A, one by B, ready to be named in the same call."""
        a_project = await _project(two_profiles["a"].owner_id(), f"a-{label}")
        a_obj = await _object(two_profiles["a"].owner_id(), a_project.id, "a.fastq")
        b_project = await _project(two_profiles["b"].owner_id(), f"b-{label}")
        b_obj = await _object(two_profiles["b"].owner_id(), b_project.id, "b.fastq")
        return a_obj, b_obj

    async def test_the_tag_update_filter_spares_a_foreign_row(
        self, monkeypatch, two_profiles
    ):
        """`_assert_all_owned` is patched out, so only the `update_many` filter
        stands between B and A's tags. Removing it fails this assertion."""
        a_obj, b_obj = await self._pair(two_profiles, "tag-filter")
        monkeypatch.setattr(
            search_service, "_assert_all_owned", AsyncMock(return_value=None)
        )

        result = await search_service.bulk_update_tags(
            [b_obj.id, a_obj.id], owner=two_profiles["b"].owner_id(), add=["b-reached"]
        )

        assert (await DataObject.get(a_obj.id)).tags == []
        # B's own row still updates, so the filter is narrowing rather than
        # rejecting everything -- the direction that fails if it over-matched.
        assert (await DataObject.get(b_obj.id)).tags == ["b-reached"]
        assert result["modified"] == 1

    async def test_the_tag_pull_filter_spares_a_foreign_row(
        self, monkeypatch, two_profiles
    ):
        """`$pull` is a second update_many with its own filter, so it needs its
        own assertion -- the `$addToSet` test above never touches it."""
        a_obj, b_obj = await self._pair(two_profiles, "pull-filter")
        await a_obj.set({"tags": ["keep-me"]})
        await b_obj.set({"tags": ["keep-me"]})
        monkeypatch.setattr(
            search_service, "_assert_all_owned", AsyncMock(return_value=None)
        )

        await search_service.bulk_update_tags(
            [b_obj.id, a_obj.id],
            owner=two_profiles["b"].owner_id(),
            remove=["keep-me"],
        )

        assert (await DataObject.get(a_obj.id)).tags == ["keep-me"]
        assert (await DataObject.get(b_obj.id)).tags == []

    async def test_the_metadata_update_filter_spares_a_foreign_row(
        self, monkeypatch, two_profiles
    ):
        a_obj, b_obj = await self._pair(two_profiles, "meta-filter")
        await a_obj.set({"metadata": {"sample_id": "P-041"}})
        monkeypatch.setattr(
            search_service, "_assert_all_owned", AsyncMock(return_value=None)
        )

        await search_service.bulk_update_metadata(
            [b_obj.id, a_obj.id],
            owner=two_profiles["b"].owner_id(),
            set_values={"sample_id": "OVERWRITTEN"},
        )

        assert (await DataObject.get(a_obj.id)).metadata["sample_id"] == "P-041"
        assert (await DataObject.get(b_obj.id)).metadata["sample_id"] == "OVERWRITTEN"

    async def test_the_metadata_unset_filter_spares_a_foreign_row(
        self, monkeypatch, two_profiles
    ):
        """`$unset` destroys rather than overwrites, so an unfiltered one takes
        a field away with nothing to restore it from."""
        a_obj, b_obj = await self._pair(two_profiles, "unset-filter")
        await a_obj.set({"metadata": {"sample_id": "P-041"}})
        await b_obj.set({"metadata": {"sample_id": "P-999"}})
        monkeypatch.setattr(
            search_service, "_assert_all_owned", AsyncMock(return_value=None)
        )

        await search_service.bulk_update_metadata(
            [b_obj.id, a_obj.id],
            owner=two_profiles["b"].owner_id(),
            unset_keys=["sample_id"],
        )

        assert (await DataObject.get(a_obj.id)).metadata["sample_id"] == "P-041"
        assert "sample_id" not in (await DataObject.get(b_obj.id)).metadata
