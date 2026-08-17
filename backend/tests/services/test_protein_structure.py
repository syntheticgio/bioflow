"""Resolving a protein accession to its deposited structures.

Every test patches the transport. None reach the network: a suite that
silently depends on UniProt being up fails for reasons having nothing to do
with this code -- the rule `test_structure_lookup.py` states for the same
reason.

The two selection tests below encode measurements taken against the live API
on 2026-08-17, recorded in the design doc. They are the reason a selection
rule exists at all rather than "take the first result".
"""

import json

import pytest
import pytest_asyncio

from app.metadata.protein_headers import ProteinRef, RefKind
from app.models import ProteinStructureLookup
from app.services import protein_structure

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest_asyncio.fixture(loop_scope="module", autouse=True)
async def clean_cache():
    """A cached row from a previous test would suppress the request that the
    next one is counting -- same trap and same fix as
    `test_structure_lookup.py`'s fixture of the same name."""
    await ProteinStructureLookup.find_all().delete()


def _entry(accession, *, pdb=(), reviewed=True, name="Enolase 1"):
    return {
        "primaryAccession": accession,
        "entryType": "UniProtKB reviewed (Swiss-Prot)"
        if reviewed
        else "UniProtKB unreviewed (TrEMBL)",
        "proteinDescription": {"recommendedName": {"fullName": {"value": name}}},
        "uniProtKBCrossReferences": [
            {"database": "PDB", "id": p} for p in pdb
        ]
        + [{"database": "STRING", "id": "ignored"}],
    }


def _patch(monkeypatch, results):
    def fake_get(url, *, timeout=None):
        fake_get.last_url = url
        return json.dumps({"results": results}).encode()

    monkeypatch.setattr(protein_structure, "_get", fake_get)
    return fake_get


async def test_uniprot_accession_resolves_to_its_pdb_ids(monkeypatch):
    _patch(monkeypatch, [_entry("P00924", pdb=["1EBG", "1EBH"])])

    hit = await protein_structure.resolve(
        ProteinRef(kind=RefKind.UNIPROT, accession="P00924")
    )

    assert hit is not None
    assert hit.accession == "P00924"
    assert hit.pdb_ids == ["1EBG", "1EBH"]
    assert hit.protein_name == "Enolase 1"


async def test_refseq_accession_queries_the_cross_reference_index(monkeypatch):
    """R15. A RefSeq ID is not a UniProt accession and cannot be looked up as one."""
    fake_get = _patch(monkeypatch, [_entry("P00924", pdb=["1EBG"])])

    await protein_structure.resolve(
        ProteinRef(kind=RefKind.REFSEQ, accession="NP_009342")
    )

    assert "xref%3Arefseq-NP_009342" in fake_get.last_url


async def test_prefers_a_reviewed_entry(monkeypatch):
    """R16. Measured: xref:refseq-NP_000537 returns three entries for TP53."""
    _patch(
        monkeypatch,
        [
            _entry("K7PPA8", pdb=[], reviewed=False, name="isoform"),
            _entry("P04637", pdb=["1A1U"], reviewed=True, name="Cellular tumor antigen p53"),
        ],
    )

    hit = await protein_structure.resolve(
        ProteinRef(kind=RefKind.REFSEQ, accession="NP_000537")
    )

    assert hit.accession == "P04637"


async def test_prefers_a_structure_bearing_entry_among_equals(monkeypatch):
    """R17. Two reviewed entries, only one with structures -- take that one."""
    _patch(
        monkeypatch,
        [
            _entry("Q00001", pdb=[], reviewed=True),
            _entry("P04637", pdb=["1A1U", "1AIE"], reviewed=True),
        ],
    )

    hit = await protein_structure.resolve(
        ProteinRef(kind=RefKind.REFSEQ, accession="NP_000537")
    )

    assert hit.accession == "P04637"


async def test_resolution_with_no_structures_is_a_hit_not_a_miss(monkeypatch):
    """R28. "Resolved but has no structure" and "did not resolve" are different
    sentences in the UI, so they must be different return values here."""
    _patch(monkeypatch, [_entry("Q00001", pdb=[])])

    hit = await protein_structure.resolve(
        ProteinRef(kind=RefKind.UNIPROT, accession="Q00001")
    )

    assert hit is not None
    assert hit.pdb_ids == []


async def test_result_is_cached_by_accession(monkeypatch):
    """R18. A second view of the same record must not re-query UniProt."""
    fake_get = _patch(monkeypatch, [_entry("P00924", pdb=["1EBG"])])
    calls = []

    def counting(url, *, timeout=None):
        calls.append(url)
        return fake_get(url, timeout=timeout)

    monkeypatch.setattr(protein_structure, "_get", counting)
    ref = ProteinRef(kind=RefKind.UNIPROT, accession="P00924")

    await protein_structure.resolve(ref)
    await protein_structure.resolve(ref)

    assert len(calls) == 1


async def test_a_miss_is_cached_too(monkeypatch):
    """R19. The no-structure case is the majority; an uncached miss means
    every view re-queries an accession that will never resolve."""
    calls = []

    def empty(url, *, timeout=None):
        calls.append(url)
        return json.dumps({"results": []}).encode()

    monkeypatch.setattr(protein_structure, "_get", empty)
    ref = ProteinRef(kind=RefKind.UNIPROT, accession="Q99999")

    assert await protein_structure.resolve(ref) is None
    assert await protein_structure.resolve(ref) is None
    assert len(calls) == 1


async def test_transport_failure_returns_none_and_does_not_raise(monkeypatch):
    """R35. A UniProt outage reports "no structure", never a 500."""

    def boom(url, *, timeout=None):
        raise TimeoutError("uniprot unreachable")

    monkeypatch.setattr(protein_structure, "_get", boom)

    assert (
        await protein_structure.resolve(
            ProteinRef(kind=RefKind.UNIPROT, accession="P00924")
        )
        is None
    )


async def test_transport_failure_is_not_cached(monkeypatch):
    """An outage must not poison the cache with a permanent miss.

    A cached failure is indistinguishable from a cached "no structure", and
    this collection has no expiry -- so caching an outage would make a
    temporary problem permanent until someone dropped the collection.
    """
    def boom(url, *, timeout=None):
        raise TimeoutError("uniprot unreachable")

    monkeypatch.setattr(protein_structure, "_get", boom)
    ref = ProteinRef(kind=RefKind.UNIPROT, accession="P00924")
    await protein_structure.resolve(ref)

    assert await ProteinStructureLookup.find_one(
        ProteinStructureLookup.accession == "P00924"
    ) is None
