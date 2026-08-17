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
from app.models import DataObject, FormatInfo, Profile, ProteinRecord
from app.services import profile_service, project_service

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]

# Project names must be unique per owner; these fixtures are function-scoped
# (loop_scope="module" only pins the event loop, per two_profiles's own note
# in tests/api/conftest.py), so a counter avoids a collision across tests.
_project_seq = itertools.count()

# `resolve_owner` treats "no header" as an error even when a profile has
# adopted "local" -- the adoption only satisfies the literal value "local",
# so requests still have to send it explicitly.
LOCAL_HEADERS = {"X-BioFlow-Profile": "local"}


@pytest_asyncio.fixture(loop_scope="module")
async def local_profile():
    """Adopts the `"local"` owner so a bare `client` call (no
    X-BioFlow-Profile header) resolves without a ProfileUnresolvedError --
    `DataObject`'s `owner` field defaults to `"local"` (TimestampedDocument),
    and `resolve_owner` only accepts that default when a profile has adopted
    it.
    """
    await Profile.find_all().delete()
    profile = await profile_service.create_profile(
        username="local-owner", is_first_boot=True
    )
    yield profile
    await profile.delete()


@pytest_asyncio.fixture(loop_scope="module")
async def protein_object(local_profile) -> DataObject:
    n = next(_project_seq)
    project = await project_service.create_project(
        name=f"protein-records-project-{n}", owner="local"
    )
    obj = DataObject(
        project_id=project.id,
        name="proteins.faa",
        format=FormatInfo(),
    )
    await obj.insert()
    return obj


@pytest_asyncio.fixture(loop_scope="module")
async def non_protein_object(local_profile) -> DataObject:
    n = next(_project_seq)
    project = await project_service.create_project(
        name=f"non-protein-records-project-{n}", owner="local"
    )
    obj = DataObject(
        project_id=project.id,
        name="reads.fastq",
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


async def test_lists_records_in_file_order(client, seeded):
    resp = await client.get(
        f"/api/v1/objects/{seeded.id}/protein-records?limit=5", headers=LOCAL_HEADERS
    )
    assert resp.status_code == 200
    body = resp.json()

    assert body["total"] == 12
    assert [r["ordinal"] for r in body["rows"]] == [0, 1, 2, 3, 4]


async def test_paging_returns_the_requested_window(client, seeded):
    resp = await client.get(
        f"/api/v1/objects/{seeded.id}/protein-records?offset=10&limit=5",
        headers=LOCAL_HEADERS,
    )
    body = resp.json()

    # Total is the whole population, not the window -- the UI shows "11-12 of 12".
    assert body["total"] == 12
    assert [r["ordinal"] for r in body["rows"]] == [10, 11]


async def test_search_matches_identifier_and_description(client, seeded):
    """R26. One query covers both fields; a user does not know which they typed."""
    by_description = await client.get(
        f"/api/v1/objects/{seeded.id}/protein-records?q=pyruvate",
        headers=LOCAL_HEADERS,
    )
    assert [r["ordinal"] for r in by_description.json()["rows"]] == [3, 7]

    by_identifier = await client.get(
        f"/api/v1/objects/{seeded.id}/protein-records?q=NP_100007",
        headers=LOCAL_HEADERS,
    )
    assert [r["ordinal"] for r in by_identifier.json()["rows"]] == [7]


async def test_search_total_reflects_the_match_not_the_file(client, seeded):
    resp = await client.get(
        f"/api/v1/objects/{seeded.id}/protein-records?q=pyruvate",
        headers=LOCAL_HEADERS,
    )
    assert resp.json()["total"] == 2


async def test_search_is_case_insensitive(client, seeded):
    resp = await client.get(
        f"/api/v1/objects/{seeded.id}/protein-records?q=PYRUVATE",
        headers=LOCAL_HEADERS,
    )
    assert resp.json()["total"] == 2


async def test_search_treats_regex_metacharacters_literally(client, seeded):
    """A user typing `NP_100003.1` must not have the dot read as a wildcard.

    The search is implemented as a Mongo regex, so an unescaped input is both
    a wrong-results bug and a way to hand the database a pathological pattern.
    """
    resp = await client.get(
        f"/api/v1/objects/{seeded.id}/protein-records?q=hypothetical.protein",
        headers=LOCAL_HEADERS,
    )
    assert resp.json()["total"] == 0


async def test_reports_no_records_for_a_file_that_has_none(client, non_protein_object):
    resp = await client.get(
        f"/api/v1/objects/{non_protein_object.id}/protein-records",
        headers=LOCAL_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 0
    assert resp.json()["rows"] == []


async def test_unknown_object_is_a_404(client, local_profile):
    resp = await client.get(
        f"/api/v1/objects/{PydanticObjectId()}/protein-records",
        headers=LOCAL_HEADERS,
    )
    assert resp.status_code == 404
