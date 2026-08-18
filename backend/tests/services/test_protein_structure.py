"""Resolving a protein accession to its deposited structures.

Every test patches the transport. None reach the network: a suite that
silently depends on UniProt being up fails for reasons having nothing to do
with this code -- the rule `test_structure_lookup.py` states for the same
reason.

The two selection tests below encode measurements taken against the live API
on 2026-08-17, recorded in the design doc. They are the reason a selection
rule exists at all rather than "take the first result".
"""

import hashlib
import json
import urllib.error

import pytest
import pytest_asyncio

from app.metadata.protein_headers import ProteinRef, RefKind
from app.models import ProteinSequenceLookup, ProteinStructureLookup
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
    await ProteinSequenceLookup.find_all().delete()


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


async def test_query_rejected_400_is_a_cached_miss(monkeypatch):
    """A 400 is UniProt saying the query is invalid -- measured: an accession
    outside its format (ZZ999999) gets "invalid format", not an empty list.
    That is a permanent answer for the accession, not an outage: it must not
    raise (the UI would offer a retry that can never help) and it is cached,
    like an empty result list."""

    def bad_request(url, *, timeout=None):
        raise urllib.error.HTTPError(url, 400, "Bad Request", None, None)

    monkeypatch.setattr(protein_structure, "_get", bad_request)
    ref = ProteinRef(kind=RefKind.UNIPROT, accession="ZZ999999")

    assert await protein_structure.resolve(ref) is None
    assert await protein_structure.resolve(ref) is None

    cached = await ProteinStructureLookup.find_one(
        ProteinStructureLookup.accession == "ZZ999999"
    )
    assert cached is not None and cached.resolved_accession is None


async def test_non_400_http_error_still_raises_unavailable(monkeypatch):
    """A 429 or 5xx is a real outage, not a rejected query: retryable, and not
    cached (the 400 branch above must not swallow it)."""

    def rate_limited(url, *, timeout=None):
        raise urllib.error.HTTPError(url, 429, "Too Many Requests", None, None)

    monkeypatch.setattr(protein_structure, "_get", rate_limited)

    with pytest.raises(protein_structure.ProteinStructureUnavailable):
        await protein_structure.resolve(
            ProteinRef(kind=RefKind.UNIPROT, accession="P00924")
        )


async def test_transport_failure_raises_unavailable(monkeypatch):
    """R35. A UniProt outage is a retryable state, never a 500 -- and never a
    None, which would read as a permanent zero-candidate answer and be
    cached as one."""

    def boom(url, *, timeout=None):
        raise TimeoutError("uniprot unreachable")

    monkeypatch.setattr(protein_structure, "_get", boom)

    with pytest.raises(protein_structure.ProteinStructureUnavailable):
        await protein_structure.resolve(
            ProteinRef(kind=RefKind.UNIPROT, accession="P00924")
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
    with pytest.raises(protein_structure.ProteinStructureUnavailable):
        await protein_structure.resolve(ref)

    assert await ProteinStructureLookup.find_one(
        ProteinStructureLookup.accession == "P00924"
    ) is None


# --- Sequence-based resolution (issue #534) ---


async def test_sequence_exact_match_resolves(monkeypatch):
    """#534. A sequence search that hits returns a StructureHit with PDB IDs."""
    _patch(monkeypatch, [_entry("P00924", pdb=["1EBG", "1EBH"])])

    hit = await protein_structure.resolve_by_sequence("MAVSKVYARSVYDSRGNPTV")

    assert hit is not None
    assert hit.accession == "P00924"
    assert hit.pdb_ids == ["1EBG", "1EBH"]
    assert hit.protein_name == "Enolase 1"


async def test_sequence_match_with_no_pdb_is_a_hit_not_a_miss(monkeypatch):
    """#534 / R28. A match that selects an entry with no PDB entries is still a
    hit -- the UI distinguishes "no structure deposited" from "nothing
    matches", and that distinction lives in a non-None return."""
    _patch(monkeypatch, [_entry("Q00001", pdb=[])])

    hit = await protein_structure.resolve_by_sequence("MAVSKVYARSVYDSRGNPTV")

    assert hit is not None
    assert hit.pdb_ids == []


async def test_sequence_no_match_is_cached(monkeypatch):
    """#534 / R19. Most de novo sequences have no exact entry in UniProt. An
    uncached miss would re-query on every view of the same protein."""
    calls = []

    def empty(url, *, timeout=None):
        calls.append(url)
        return json.dumps({"results": []}).encode()

    monkeypatch.setattr(protein_structure, "_get", empty)

    assert await protein_structure.resolve_by_sequence("MKKLLA") is None
    assert await protein_structure.resolve_by_sequence("MKKLLA") is None
    assert len(calls) == 1


async def test_sequence_outage_is_not_cached(monkeypatch):
    """#534. An outage must not poison the cache -- a cached failure is
    indistinguishable from a cached miss, and this collection has no expiry."""
    def boom(url, *, timeout=None):
        raise TimeoutError("uniprot unreachable")

    monkeypatch.setattr(protein_structure, "_get", boom)

    assert await protein_structure.resolve_by_sequence("MKKLLA") is None
    assert (
        await ProteinSequenceLookup.find_one(
            ProteinSequenceLookup.sequence_hash
            == hashlib.sha256(b"MKKLLA").hexdigest()
        )
        is None
    )


async def test_sequence_search_uses_sequence_query_field(monkeypatch):
    """#534. The query must use UniProt's ``sequence:`` field, not the
    accession query from the accession path."""
    fake_get = _patch(monkeypatch, [_entry("P00924", pdb=["1EBG"])])

    await protein_structure.resolve_by_sequence("MAVSKVYARSVYDSRGNPTV")

    assert 'sequence%3A%22MAVSKVYARSVYDSRGNPTV%22' in fake_get.last_url


async def test_sequence_result_is_cached(monkeypatch):
    """#534. A successful match is cached; the second call must not hit
    UniProt again."""
    fake_get = _patch(monkeypatch, [_entry("P00924", pdb=["1EBG"])])
    calls = []

    def counting(url, *, timeout=None):
        calls.append(url)
        return fake_get(url, timeout=timeout)

    monkeypatch.setattr(protein_structure, "_get", counting)

    await protein_structure.resolve_by_sequence("MAVSKVYARSVYDSRGNPTV")
    await protein_structure.resolve_by_sequence("MAVSKVYARSVYDSRGNPTV")

    assert len(calls) == 1
