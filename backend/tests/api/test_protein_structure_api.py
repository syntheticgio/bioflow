"""One record's structure, and the four states it can be in.

The states are explicit in the response rather than inferred from nulls,
because the UI says four genuinely different things and a client deriving
them from `accession is None` gets "no structure deposited" and "header names
nothing" backwards -- which is exactly the confusion the existing variants
modal's comments warn about.
"""

import itertools

import pytest
import pytest_asyncio

from app.metadata.protein_headers import RefKind
from app.models import DataObject, FormatInfo, ProteinRecord
from app.services import project_service, protein_structure

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
        name=f"protein-structure-project-{n}", owner=owner
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
async def records(protein_object):
    await ProteinRecord.insert_many(
        [
            ProteinRecord(
                object_id=protein_object.id,
                ordinal=0,
                identifier="NP_009342.1",
                description="Cdc19p",
                length=500,
                byte_offset=0,
                ref_kind=RefKind.REFSEQ,
                ref_accession="NP_009342",
            ),
            ProteinRecord(
                object_id=protein_object.id,
                ordinal=1,
                identifier="KLLIPMDF_00023",
                description="hypothetical protein",
                length=143,
                byte_offset=600,
            ),
        ]
    )
    return protein_object


async def test_resolved_record_returns_pdb_ids(client, records, two_profiles, monkeypatch):
    async def fake_resolve(ref):
        return protein_structure.StructureHit(
            accession="P00549", protein_name="Pyruvate kinase 1", pdb_ids=["1A3W"]
        )

    monkeypatch.setattr(protein_structure, "resolve", fake_resolve)

    resp = await client.get(
        f"/api/v1/objects/{records.id}/protein-records/0/structure",
        headers=two_profiles["a_headers"],
    )
    body = resp.json()

    assert body["state"] == "resolved"
    assert body["pdb_ids"] == ["1A3W"]
    # R21: the resolved accession and name are surfaced so a mis-resolution is
    # visible to the reader rather than silent.
    assert body["accession"] == "P00549"
    assert body["protein_name"] == "Pyruvate kinase 1"


async def test_resolved_but_structureless_record_is_its_own_state(
    client, records, two_profiles, monkeypatch
):
    """R28. The majority outcome, and it must not read as a failure."""

    async def fake_resolve(ref):
        return protein_structure.StructureHit(
            accession="P00549", protein_name="Pyruvate kinase 1", pdb_ids=[]
        )

    monkeypatch.setattr(protein_structure, "resolve", fake_resolve)

    resp = await client.get(
        f"/api/v1/objects/{records.id}/protein-records/0/structure",
        headers=two_profiles["a_headers"],
    )

    assert resp.json()["state"] == "no_structure"
    assert resp.json()["accession"] == "P00549"


async def test_record_naming_no_accession_never_queries_uniprot(
    client, records, two_profiles, monkeypatch
):
    """R27. There is nothing to look up, so looking anyway is a wasted call."""
    called = []

    async def fake_resolve(ref):
        called.append(ref)
        return None

    monkeypatch.setattr(protein_structure, "resolve", fake_resolve)

    resp = await client.get(
        f"/api/v1/objects/{records.id}/protein-records/1/structure",
        headers=two_profiles["a_headers"],
    )

    assert resp.json()["state"] == "no_reference"
    assert called == []


async def test_lookup_failure_is_distinct_from_no_structure(
    client, records, two_profiles, monkeypatch
):
    """R22. An outage is retryable; "no structure deposited" is not."""

    async def fake_resolve(ref):
        return None

    monkeypatch.setattr(protein_structure, "resolve", fake_resolve)

    resp = await client.get(
        f"/api/v1/objects/{records.id}/protein-records/0/structure",
        headers=two_profiles["a_headers"],
    )

    assert resp.status_code == 200
    assert resp.json()["state"] == "lookup_failed"


async def test_unknown_ordinal_is_a_404(client, records, two_profiles):
    resp = await client.get(
        f"/api/v1/objects/{records.id}/protein-records/999/structure",
        headers=two_profiles["a_headers"],
    )
    assert resp.status_code == 404


async def test_other_owner_cannot_read_the_record(client, records, two_profiles):
    resp = await client.get(
        f"/api/v1/objects/{records.id}/protein-records/0/structure",
        headers=two_profiles["b_headers"],
    )
    assert resp.status_code == 404
