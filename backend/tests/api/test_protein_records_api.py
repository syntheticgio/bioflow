"""The protein record listing endpoint.

Paging and search are one endpoint because they are one question -- "which of
this file's proteins am I looking at" -- and splitting them would mean the
client picks between two routes on whether a search box is empty.
"""

import itertools

import pytest
import pytest_asyncio
from beanie import PydanticObjectId

from app.metadata.protein_headers import RefKind
from app.models import DataObject, FormatInfo, ProteinRecord
from app.services import project_service

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]

# Project names must be unique per owner; these fixtures are function-scoped
# (loop_scope="module" only pins the event loop, per two_profiles's own note
# in tests/api/conftest.py), so a counter avoids a collision across tests.
_project_seq = itertools.count()


@pytest_asyncio.fixture(loop_scope="module")
async def protein_object(two_profiles) -> DataObject:
    n = next(_project_seq)
    owner = two_profiles["a"].owner_id()
    project = await project_service.create_project(
        name=f"protein-records-project-{n}", owner=owner
    )
    obj = DataObject(
        project_id=project.id,
        name="proteins.faa",
        owner=owner,
        format=FormatInfo(),
    )
    await obj.insert()
    return obj


@pytest_asyncio.fixture(loop_scope="module")
async def non_protein_object(two_profiles) -> DataObject:
    n = next(_project_seq)
    owner = two_profiles["a"].owner_id()
    project = await project_service.create_project(
        name=f"non-protein-records-project-{n}", owner=owner
    )
    obj = DataObject(
        project_id=project.id,
        name="reads.fastq",
        owner=owner,
        format=FormatInfo(),
    )
    await obj.insert()
    return obj


@pytest_asyncio.fixture(loop_scope="module")
async def seeded(protein_object):
    """Twelve records, two of which are findable by description."""
    records = [
        ProteinRecord(
            object_id=protein_object.id,
            ordinal=i,
            identifier=f"NP_{100000 + i}",
            description="pyruvate kinase" if i in (3, 7) else "hypothetical protein",
            length=200 + i,
            byte_offset=i * 100,
            ref_kind=RefKind.REFSEQ,
            ref_accession=f"NP_{100000 + i}",
        )
        for i in range(12)
    ]
    await ProteinRecord.insert_many(records)
    return protein_object


async def test_lists_records_in_file_order(client, seeded, two_profiles):
    resp = await client.get(
        f"/api/v1/objects/{seeded.id}/protein-records?limit=5",
        headers=two_profiles["a_headers"],
    )
    assert resp.status_code == 200
    body = resp.json()

    assert body["total"] == 12
    assert [r["ordinal"] for r in body["rows"]] == [0, 1, 2, 3, 4]


async def test_paging_returns_the_requested_window(client, seeded, two_profiles):
    resp = await client.get(
        f"/api/v1/objects/{seeded.id}/protein-records?offset=10&limit=5",
        headers=two_profiles["a_headers"],
    )
    body = resp.json()

    # Total is the whole population, not the window -- the UI shows "11-12 of 12".
    assert body["total"] == 12
    assert [r["ordinal"] for r in body["rows"]] == [10, 11]


async def test_search_matches_identifier_and_description(client, seeded, two_profiles):
    """R26. One query covers both fields; a user does not know which they typed."""
    by_description = await client.get(
        f"/api/v1/objects/{seeded.id}/protein-records?q=pyruvate",
        headers=two_profiles["a_headers"],
    )
    assert [r["ordinal"] for r in by_description.json()["rows"]] == [3, 7]

    by_identifier = await client.get(
        f"/api/v1/objects/{seeded.id}/protein-records?q=NP_100007",
        headers=two_profiles["a_headers"],
    )
    assert [r["ordinal"] for r in by_identifier.json()["rows"]] == [7]


async def test_search_total_reflects_the_match_not_the_file(
    client, seeded, two_profiles
):
    resp = await client.get(
        f"/api/v1/objects/{seeded.id}/protein-records?q=pyruvate",
        headers=two_profiles["a_headers"],
    )
    assert resp.json()["total"] == 2


async def test_search_is_case_insensitive(client, seeded, two_profiles):
    resp = await client.get(
        f"/api/v1/objects/{seeded.id}/protein-records?q=PYRUVATE",
        headers=two_profiles["a_headers"],
    )
    assert resp.json()["total"] == 2


async def test_search_treats_regex_metacharacters_literally(
    client, seeded, two_profiles
):
    """A user typing `NP_100003.1` must not have the dot read as a wildcard.

    The search is implemented as a Mongo regex, so an unescaped input is both
    a wrong-results bug and a way to hand the database a pathological pattern.
    """
    resp = await client.get(
        f"/api/v1/objects/{seeded.id}/protein-records?q=hypothetical.protein",
        headers=two_profiles["a_headers"],
    )
    assert resp.json()["total"] == 0


async def test_reports_no_records_for_a_file_that_has_none(
    client, non_protein_object, two_profiles
):
    resp = await client.get(
        f"/api/v1/objects/{non_protein_object.id}/protein-records",
        headers=two_profiles["a_headers"],
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 0
    assert resp.json()["rows"] == []


async def test_indexed_is_false_when_indexing_has_never_run(
    client, non_protein_object, two_profiles
):
    """Finding 2: a role set to Protein after ingest never triggers indexing,
    so the endpoint must say "never indexed" rather than let an empty `rows`
    read as "no records match your search" -- the two need different copy on
    the client and this field is what tells them apart.
    """
    resp = await client.get(
        f"/api/v1/objects/{non_protein_object.id}/protein-records",
        headers=two_profiles["a_headers"],
    )
    assert resp.status_code == 200
    assert resp.json()["indexed"] is False


async def test_indexed_is_true_once_the_ingest_fact_is_set(
    client, seeded, two_profiles
):
    """`seeded` inserts ProteinRecord rows directly without going through
    ingest_headers, so its object never got the `protein_records_indexed`
    fact -- setting it here isolates the field under test from the row count,
    which a fixture with the fact set and zero rows would conflate.
    """
    seeded.facts["protein_records_indexed"] = 12
    await seeded.save()

    resp = await client.get(
        f"/api/v1/objects/{seeded.id}/protein-records",
        headers=two_profiles["a_headers"],
    )
    assert resp.status_code == 200
    assert resp.json()["indexed"] is True


async def test_unknown_object_is_a_404(client, two_profiles):
    resp = await client.get(
        f"/api/v1/objects/{PydanticObjectId()}/protein-records",
        headers=two_profiles["a_headers"],
    )
    assert resp.status_code == 404


async def test_another_profile_cannot_read_protein_records(
    client, seeded, two_profiles
):
    """Same shape as test_object_computations.py's
    TestOwnerScoping::test_another_profile_cannot_read_computations -- a
    wrong-owner request must 404, not just an unknown id."""
    mine = await client.get(
        f"/api/v1/objects/{seeded.id}/protein-records",
        headers=two_profiles["a_headers"],
    )
    theirs = await client.get(
        f"/api/v1/objects/{seeded.id}/protein-records",
        headers=two_profiles["b_headers"],
    )

    assert mine.status_code == 200
    assert theirs.status_code == 404
