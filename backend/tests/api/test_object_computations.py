"""GET /objects/{id}/computations: the per-object provenance route (#9).

`JobRunTiming` has no owner field, so the route's authorization is entirely
`object_service.get_object` -- the object fetch either raises before any
timing row is read, or it doesn't. Profile B asking for profile A's object is
therefore the assertion that matters here, per CLAUDE.md's note on asserting
the direction that fails when a seam breaks: a positive-only test would pass
whether or not owner scoping ever ran.
"""

from datetime import UTC, datetime, timedelta

import pytest
from app.models import DataObject, FormatInfo, JobRunTiming
from app.models.timing import RunOutcome
from app.services import project_service
from beanie import PydanticObjectId

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


async def _project(owner: str, name: str):
    return await project_service.create_project(name=name, owner=owner)


async def _object(
    owner: str, project_id: PydanticObjectId, name: str, *, produced_by_job=None
) -> DataObject:
    obj = DataObject(
        project_id=project_id,
        name=name,
        owner=owner,
        format=FormatInfo(),
        produced_by_job=produced_by_job,
    )
    await obj.insert()
    return obj


async def _record(
    *,
    object_id: str,
    outcome: str = RunOutcome.SUCCEEDED,
    job_id: str | None = None,
    finished_at: datetime | None = None,
    **kwargs,
) -> JobRunTiming:
    row = JobRunTiming(
        job_type="align_reads",
        input_bytes=1_000,
        duration_ms=100,
        outcome=outcome,
        object_id=object_id,
        job_id=job_id,
        finished_at=finished_at or datetime.now(UTC),
        **kwargs,
    )
    await row.insert()
    return row


class TestOwnerScoping:
    async def test_another_profile_cannot_read_computations(self, client, two_profiles):
        project = await _project(two_profiles["a"].owner_id(), "a-computations")
        obj = await _object(two_profiles["a"].owner_id(), project.id, "reads.fastq")
        await _record(object_id=str(obj.id))

        mine = await client.get(
            f"/api/v1/objects/{obj.id}/computations", headers=two_profiles["a_headers"]
        )
        theirs = await client.get(
            f"/api/v1/objects/{obj.id}/computations", headers=two_profiles["b_headers"]
        )

        assert mine.status_code == 200
        assert theirs.status_code == 404


class TestRecords:
    async def test_records_are_newest_first(self, client, two_profiles):
        owner = two_profiles["a"].owner_id()
        project = await _project(owner, "newest-first")
        obj = await _object(owner, project.id, "reads.fastq")
        base = datetime.now(UTC)
        await _record(object_id=str(obj.id), finished_at=base)
        await _record(object_id=str(obj.id), finished_at=base + timedelta(seconds=10))

        r = await client.get(
            f"/api/v1/objects/{obj.id}/computations", headers=two_profiles["a_headers"]
        )

        assert r.status_code == 200
        records = r.json()["records"]
        assert len(records) == 2
        assert records[0]["finished_at"] > records[1]["finished_at"]

    async def test_a_failed_record_is_present(self, client, two_profiles):
        """The reason `records_for_object` exists at all -- every other
        reader of this collection filters failures out."""
        owner = two_profiles["a"].owner_id()
        project = await _project(owner, "failed-record")
        obj = await _object(owner, project.id, "reads.fastq")
        await _record(object_id=str(obj.id), outcome=RunOutcome.SUCCEEDED)
        await _record(object_id=str(obj.id), outcome=RunOutcome.FAILED)

        r = await client.get(
            f"/api/v1/objects/{obj.id}/computations", headers=two_profiles["a_headers"]
        )

        outcomes = {row["outcome"] for row in r.json()["records"]}
        assert outcomes == {RunOutcome.SUCCEEDED, RunOutcome.FAILED}

    async def test_null_resource_fields_serialize_as_null_not_zero(self, client, two_profiles):
        """A run under the 60s sampling floor has no RSS measurement. A test
        that only checks the happy path would let an `or 0` coercion pass."""
        owner = two_profiles["a"].owner_id()
        project = await _project(owner, "null-resources")
        obj = await _object(owner, project.id, "reads.fastq")
        await _record(object_id=str(obj.id))

        r = await client.get(
            f"/api/v1/objects/{obj.id}/computations", headers=two_profiles["a_headers"]
        )

        record = r.json()["records"][0]
        assert record["peak_rss_bytes"] is None
        assert record["threads"] is None
        assert record["tool"] is None

    async def test_records_scoped_to_this_object_only(self, client, two_profiles):
        owner = two_profiles["a"].owner_id()
        project = await _project(owner, "scoped")
        obj = await _object(owner, project.id, "reads.fastq")
        other = await _object(owner, project.id, "other.fastq")
        await _record(object_id=str(obj.id))
        await _record(object_id=str(other.id))

        r = await client.get(
            f"/api/v1/objects/{obj.id}/computations", headers=two_profiles["a_headers"]
        )

        assert len(r.json()["records"]) == 1

    async def test_no_records_returns_an_empty_list_not_an_error(self, client, two_profiles):
        owner = two_profiles["a"].owner_id()
        project = await _project(owner, "empty")
        obj = await _object(owner, project.id, "reads.fastq")

        r = await client.get(
            f"/api/v1/objects/{obj.id}/computations", headers=two_profiles["a_headers"]
        )

        assert r.status_code == 200
        assert r.json() == {
            "produced_by": None,
            "produced_by_job": None,
            "records": [],
            "has_more": False,
        }


class TestPagination:
    async def test_has_more_true_past_the_limit(self, client, two_profiles):
        owner = two_profiles["a"].owner_id()
        project = await _project(owner, "paginated")
        obj = await _object(owner, project.id, "reads.fastq")
        for _ in range(3):
            await _record(object_id=str(obj.id))

        r = await client.get(
            f"/api/v1/objects/{obj.id}/computations?limit=2",
            headers=two_profiles["a_headers"],
        )

        body = r.json()
        assert len(body["records"]) == 2
        assert body["has_more"] is True

    async def test_has_more_false_at_exactly_the_limit(self, client, two_profiles):
        owner = two_profiles["a"].owner_id()
        project = await _project(owner, "paginated-exact")
        obj = await _object(owner, project.id, "reads.fastq")
        for _ in range(2):
            await _record(object_id=str(obj.id))

        r = await client.get(
            f"/api/v1/objects/{obj.id}/computations?limit=2",
            headers=two_profiles["a_headers"],
        )

        body = r.json()
        assert len(body["records"]) == 2
        assert body["has_more"] is False


class TestProducedBy:
    async def test_resolves_from_produced_by_job_to_the_matching_record(
        self, client, two_profiles
    ):
        owner = two_profiles["a"].owner_id()
        project = await _project(owner, "produced-by")
        producing_job_id = str(PydanticObjectId())
        obj = await _object(
            owner,
            project.id,
            "aligned.bam",
            produced_by_job=PydanticObjectId(producing_job_id),
        )
        await _record(
            object_id=str(PydanticObjectId()),  # the record's own object is the *input*
            job_id=producing_job_id,
        )
        # An unrelated record on this object, to prove produced_by isn't just
        # "the first record found".
        await _record(object_id=str(obj.id))

        r = await client.get(
            f"/api/v1/objects/{obj.id}/computations", headers=two_profiles["a_headers"]
        )

        body = r.json()
        assert body["produced_by"] is not None
        assert body["produced_by"]["job_id"] == producing_job_id
        assert body["produced_by_job"] == producing_job_id

    async def test_null_when_no_record_carries_that_job_id(self, client, two_profiles):
        """The common real case: an object created before computation
        records started recording job_id (2026-08-03) still carries
        `produced_by_job`, but no `JobRunTiming` row names it."""
        owner = two_profiles["a"].owner_id()
        project = await _project(owner, "produced-by-missing")
        obj = await _object(
            owner,
            project.id,
            "old.bam",
            produced_by_job=PydanticObjectId(),
        )

        r = await client.get(
            f"/api/v1/objects/{obj.id}/computations", headers=two_profiles["a_headers"]
        )

        body = r.json()
        assert body["produced_by"] is None
        assert body["produced_by_job"] is not None
