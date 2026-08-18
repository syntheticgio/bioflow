"""One record's structure, and the six states it can be in.

The states are explicit in the response rather than inferred from nulls,
because the UI says six genuinely different things and a client deriving
them from `accession is None` gets "no structure deposited" and "header names
nothing" backwards -- which is exactly the confusion the existing variants
modal's comments warn about.
"""

import hashlib
import itertools

import pytest
import pytest_asyncio

from app.metadata.protein_headers import RefKind
from app.models import (
    Blob,
    BlobState,
    BlobStorage,
    DataObject,
    FormatInfo,
    ProteinRecord,
    ProteinSequenceLookup,
)
from app.services import project_service, protein_record_index, protein_structure

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]

_TEST_BLOB_SHA = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
_SEQUENCE = "MKKLLA"


def _async_hit(accession, pdb_ids, protein_name):
    """Build a fake ``resolve``/``resolve_by_sequence`` that returns a hit."""

    async def _inner(*_args, **_kwargs):
        return protein_structure.StructureHit(
            accession=accession,
            protein_name=protein_name,
            pdb_ids=pdb_ids,
        )

    return _inner


async def _async_none(*_args, **_kwargs):
    return None

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
        blob_sha256=_TEST_BLOB_SHA,
    )
    await obj.insert()
    if await Blob.find_one(Blob.id == _TEST_BLOB_SHA) is None:
        await Blob(
            id=_TEST_BLOB_SHA,
            size=999,
            state=BlobState.PRESENT,
            storage=BlobStorage.MANAGED,
        ).insert()
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


@pytest_asyncio.fixture(loop_scope="module", autouse=True)
async def clean_sequence_cache():
    """A cached miss from one test would suppress the request the next is
    counting -- same trap and same fix as the structure_lookup tests' fixture."""
    await ProteinSequenceLookup.find_all().delete()


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
    """R27. A record with no accession falls back to sequence search, but if the
    underlying file is not on disk (no stored blob path, or the blob file is
    gone), there is nothing to search and the accession path is never tried."""
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
        raise protein_structure.ProteinStructureUnavailable("uniprot unreachable")

    monkeypatch.setattr(protein_structure, "resolve", fake_resolve)

    resp = await client.get(
        f"/api/v1/objects/{records.id}/protein-records/0/structure",
        headers=two_profiles["a_headers"],
    )

    assert resp.status_code == 200
    assert resp.json()["state"] == "lookup_failed"


async def test_zero_candidate_is_its_own_state(
    client, records, two_profiles, monkeypatch
):
    """A query that succeeded and matched nothing is permanent -- the UI must
    not offer a retry for it, so it cannot share lookup_failed's state.
    lookup_failed means the request to UniProt itself failed."""

    async def fake_resolve(ref):
        return None

    monkeypatch.setattr(protein_structure, "resolve", fake_resolve)

    resp = await client.get(
        f"/api/v1/objects/{records.id}/protein-records/0/structure",
        headers=two_profiles["a_headers"],
    )

    assert resp.status_code == 200
    assert resp.json()["state"] == "no_candidate"
    assert resp.json()["accession"] is None
    assert resp.json()["pdb_ids"] == []


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


# --- Sequence fallback (issue #534) ---


async def test_sequence_fallback_resolves(
    client, records, two_profiles, monkeypatch
):
    """#534. A record with no accession is resolved by querying UniProt for an
    exact sequence match found in the file."""
    monkeypatch.setattr(
        protein_record_index, "read_record_sequence", lambda *a, **kw: _SEQUENCE
    )
    monkeypatch.setattr(
        protein_structure,
        "resolve_by_sequence",
        _async_hit("P68930", ["1ABC"], "Hypothetical kinase"),
    )

    resp = await client.get(
        f"/api/v1/objects/{records.id}/protein-records/1/structure",
        headers=two_profiles["a_headers"],
    )
    body = resp.json()

    assert body["state"] == "resolved"
    assert body["accession"] == "P68930"
    assert body["protein_name"] == "Hypothetical kinase"
    assert body["pdb_ids"] == ["1ABC"]


async def test_sequence_fallback_no_structure(
    client, records, two_profiles, monkeypatch
):
    """#534. A sequence match with no PDB entries is ``no_structure``, not a
    failure or a miss -- the lookup succeeded but nothing was deposited."""
    monkeypatch.setattr(
        protein_record_index, "read_record_sequence", lambda *a, **kw: _SEQUENCE
    )
    monkeypatch.setattr(
        protein_structure,
        "resolve_by_sequence",
        _async_hit("P68930", [], "Hypothetical kinase"),
    )

    resp = await client.get(
        f"/api/v1/objects/{records.id}/protein-records/1/structure",
        headers=two_profiles["a_headers"],
    )

    assert resp.json()["state"] == "no_structure"
    assert resp.json()["accession"] == "P68930"


async def test_sequence_fallback_no_match(
    client, records, two_profiles, monkeypatch
):
    """#534. ``resolve_by_sequence`` returns None and a negative cache entry
    exists → ``no_sequence_match``."""
    monkeypatch.setattr(
        protein_record_index, "read_record_sequence", lambda *a, **kw: _SEQUENCE
    )
    monkeypatch.setattr(protein_structure, "resolve_by_sequence", _async_none)

    seq_hash = hashlib.sha256(_SEQUENCE.encode("utf-8")).hexdigest()
    await ProteinSequenceLookup(
        sequence_hash=seq_hash,
        resolved_accession=None,
        protein_name=None,
        pdb_ids=[],
    ).insert()

    resp = await client.get(
        f"/api/v1/objects/{records.id}/protein-records/1/structure",
        headers=two_profiles["a_headers"],
    )

    assert resp.json()["state"] == "no_sequence_match"


async def test_sequence_fallback_outage_is_lookup_failed(
    client, records, two_profiles, monkeypatch
):
    """#534. ``resolve_by_sequence`` returns None but no cache entry exists →
    ``lookup_failed`` (transient, retryable)."""
    monkeypatch.setattr(
        protein_record_index, "read_record_sequence", lambda *a, **kw: _SEQUENCE
    )
    monkeypatch.setattr(protein_structure, "resolve_by_sequence", _async_none)

    resp = await client.get(
        f"/api/v1/objects/{records.id}/protein-records/1/structure",
        headers=two_profiles["a_headers"],
    )

    assert resp.json()["state"] == "lookup_failed"


async def test_sequence_fallback_file_missing_is_no_reference(
    client, records, two_profiles, monkeypatch
):
    """#534. If the blob file is gone, the fallback degrades to
    ``no_reference`` -- the same state the old code returned, and the one the
    UI already knows to show when there is nothing to identify."""
    seen = []

    def boom(*args, **kwargs):
        seen.append(args)
        raise FileNotFoundError("blob file is gone")

    monkeypatch.setattr(protein_record_index, "read_record_sequence", boom)

    resp = await client.get(
        f"/api/v1/objects/{records.id}/protein-records/1/structure",
        headers=two_profiles["a_headers"],
    )

    assert resp.json()["state"] == "no_reference"
    assert len(seen) == 1


async def test_accession_path_still_works(client, records, two_profiles, monkeypatch):
    """#534. A record that *does* name an accession is still resolved by that
    accession, not by sequence -- the fallback is only for the unnamed rows."""
    resolved_by_sequence = []

    async def spy(seq):
        resolved_by_sequence.append(seq)
        return None

    monkeypatch.setattr(protein_structure, "resolve_by_sequence", spy)
    monkeypatch.setattr(
        protein_structure,
        "resolve",
        _async_hit("P00549", ["1A3W"], "Pyruvate kinase 1"),
    )

    resp = await client.get(
        f"/api/v1/objects/{records.id}/protein-records/0/structure",
        headers=two_profiles["a_headers"],
    )
    body = resp.json()

    assert body["state"] == "resolved"
    assert body["accession"] == "P00549"
    assert body["pdb_ids"] == ["1A3W"]
    assert resolved_by_sequence == []
