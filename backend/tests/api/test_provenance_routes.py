"""The two routes.

Split deliberately: the report route never touches a provider, so a missing
or broken AI configuration cannot take down the thing users actually cite.

Uses the `client` and `two_profiles` fixtures from `tests/api/conftest.py`.
Two profiles is the minimum that proves isolation: profile A asking for its
own data succeeds whether or not the route ever applied an owner filter, so
the isolation assertion has to be B asking for A's object.
"""

import pytest
from beanie import PydanticObjectId
from httpx import AsyncClient

from app.models.object import DataObject, ObjectRole, ObjectStatus

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


async def _obj(owner, name, **kw):
    obj = DataObject(
        project_id=PydanticObjectId(),
        name=name,
        owner=owner,
        status=ObjectStatus.READY,
        **kw,
    )
    await obj.insert()
    return obj


async def test_report_route_returns_markdown_and_gap_count(
    client: AsyncClient, two_profiles
):
    owner = two_profiles["a"].owner_id()
    reads = await _obj(owner, "reads.fastq.gz")
    bam = await _obj(
        owner,
        "aligned.bam",
        role=ObjectRole.ALIGNMENT,
        derived_from=[reads.id],
        facts={"aligned_by": "bwa-mem2"},
        # A step only renders when `produced_by_job` is set -- the walker
        # anchors steps on the job, not on facts alone (see
        # provenance_walker.py's module docstring).
        produced_by_job=PydanticObjectId(),
    )

    resp = await client.get(
        f"/api/v1/objects/{bam.id}/provenance-narrative",
        headers=two_profiles["a_headers"],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "bwa-mem2" in body["markdown"]
    assert body["gap_count"] >= 1
    assert body["steps"]


async def test_report_route_works_with_no_ai_provider(
    client: AsyncClient, two_profiles
):
    """The structured half must never depend on a configured provider."""
    obj = await _obj(two_profiles["a"].owner_id(), "reads.fastq.gz")
    resp = await client.get(
        f"/api/v1/objects/{obj.id}/provenance-narrative",
        headers=two_profiles["a_headers"],
    )
    assert resp.status_code == 200


async def test_report_route_rejects_another_owner(
    client: AsyncClient, two_profiles
):
    """B asking for A's object. `get_object` raises the same NotFoundError
    for a wrong owner as for a missing id, so this is a 404 rather than a
    403 -- deliberate, so one profile cannot confirm another's id exists."""
    obj = await _obj(two_profiles["a"].owner_id(), "secret.fastq.gz")
    resp = await client.get(
        f"/api/v1/objects/{obj.id}/provenance-narrative",
        headers=two_profiles["b_headers"],
    )
    assert resp.status_code == 404


async def test_prose_route_reports_unavailable_with_no_provider(
    client: AsyncClient, two_profiles
):
    obj = await _obj(two_profiles["a"].owner_id(), "reads.fastq.gz")
    resp = await client.post(
        f"/api/v1/objects/{obj.id}/provenance-narrative/prose",
        headers=two_profiles["a_headers"],
    )
    assert resp.status_code == 200
    assert resp.json()["prose"] is None
    assert resp.json()["unavailable_reason"]


async def test_route_serves_the_structured_lineage_the_tab_renders(
    client: AsyncClient, two_profiles
):
    """The History tab renders from `lineage`, not from `markdown` -- the
    markdown backs Copy report only. A response that carried the facts in
    prose alone would leave the tab parsing rendered text."""
    owner = two_profiles["a"].owner_id()
    reads = await _obj(owner, "reads.fastq.gz")
    bam = await _obj(
        owner,
        "aligned.bam",
        role=ObjectRole.ALIGNMENT,
        derived_from=[reads.id],
        facts={"aligned_by": "bwa-mem2", "align_params": {"threads": 4}},
        produced_by_job=PydanticObjectId(),
    )

    resp = await client.get(
        f"/api/v1/objects/{bam.id}/provenance-narrative",
        headers=two_profiles["a_headers"],
    )
    body = resp.json()

    assert [s["name"] for s in body["lineage"]] == ["reads.fastq.gz", "aligned.bam"]
    step = body["lineage"][-1]
    assert step["params"] == {"threads": 4}
    # The version was never recorded, and the row says so where the version
    # would have been rather than staying silent.
    assert any("version" in g for g in step["gaps"])


async def test_route_labels_gaps_for_the_rail(client: AsyncClient, two_profiles):
    """Each gap needs a standalone phrase: the rail lists them away from the
    step they belong to."""
    owner = two_profiles["a"].owner_id()
    reads = await _obj(
        owner, "DRR1066343_1.fastq", produced_by_job=PydanticObjectId()
    )

    resp = await client.get(
        f"/api/v1/objects/{reads.id}/provenance-narrative",
        headers=two_profiles["a_headers"],
    )
    body = resp.json()

    assert body["gaps"]
    assert all(g["label"] for g in body["gaps"])


async def test_two_mates_from_one_job_are_one_lineage_row(
    client: AsyncClient, two_profiles
):
    """The layout's motivating case, end to end."""
    owner = two_profiles["a"].owner_id()
    job = PydanticObjectId()
    mate_1 = await _obj(owner, "DRR1066343_1.fastq", produced_by_job=job)
    mate_2 = await _obj(owner, "DRR1066343_2.fastq", produced_by_job=job)
    trimmed = await _obj(
        owner,
        "DRR1066343.trimmed.fastq",
        derived_from=[mate_1.id, mate_2.id],
        facts={"trimmed_by": "trimmomatic"},
        produced_by_job=PydanticObjectId(),
    )

    resp = await client.get(
        f"/api/v1/objects/{trimmed.id}/provenance-narrative",
        headers=two_profiles["a_headers"],
    )
    body = resp.json()

    names = [s["name"] for s in body["lineage"]]
    assert "DRR1066343_1.fastq and DRR1066343_2.fastq" in names
    assert len(body["lineage"]) == 2
