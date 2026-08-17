"""Resolving a gene to a protein structure.

The premise of this module is that a gene symbol is not an identifier. It is a
label that UniProt may attach to more than one protein, and the wrong choice is
not a worse answer but a false one -- a structure of some other protein with a
residue highlighted at a position that means nothing.

The measured case that motivates every guard here is SRP1. Queried against
yeast, UniProt returns TIR1 (P10863, 254aa) *first*, because TIR1 lists SRP1 as
an alias; the intended protein, importin subunit alpha (Q02821, 542aa), ranks
second. The callset's SRP1 variants reach residue 428, which fits Q02821 and
not P10863 -- so comparing aa_pos against the sequence length is what tells the
two apart, and is the reason the resolver reads the length at all.

Every test here patches the transport. None of them reach the network: a suite
that silently depends on UniProt being up is a suite that fails for reasons
having nothing to do with this code.
"""

import json

import pytest
import pytest_asyncio

from app.models import StructureLookup
from app.services import structure_lookup

# The resolver caches through Beanie, so the models have to be initialised for
# `StructureLookup.gene` to resolve as a query expression at all. `beanie_models`
# is module-scoped and holds a Motor connection bound to that scope's loop, so
# these run on the same one -- see test_annotation_inputs.py for the same note.
pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


def _entry(accession, length, pdbs=()):
    """One UniProt search result, in the shape the REST API returns."""
    return {
        "primaryAccession": accession,
        "sequence": {"length": length},
        "uniProtKBCrossReferences": [
            {"database": "PDB", "id": p} for p in pdbs
        ],
    }


def _payload(*entries):
    return json.dumps({"results": list(entries)}).encode()


@pytest_asyncio.fixture(loop_scope="module", autouse=True)
async def clean_cache():
    """A cached row from a previous test would suppress the request that the
    next one is counting."""
    await StructureLookup.find_all().delete()


@pytest.fixture
def fake_uniprot(monkeypatch):
    """Replace the HTTP call, recording every URL requested."""
    calls = []

    def _fetch(url, *, timeout):
        calls.append(url)
        return _fetch.payload

    _fetch.payload = _payload()
    monkeypatch.setattr(structure_lookup, "_get", _fetch)
    _fetch.calls = calls
    return _fetch


# --- The SRP1 case -----------------------------------------------------------


async def test_skips_a_candidate_too_short_for_the_residue(fake_uniprot):
    """The regression test for the whole design.

    Without this the resolver returns TIR1, and every SRP1 variant opens a
    structure of an unrelated protein. Nothing else in the system would notice.
    """
    fake_uniprot.payload = _payload(
        _entry("P10863", 254),                      # TIR1, ranked first
        _entry("Q02821", 542, ["1BK5", "1EE4"]),    # importin alpha
    )

    hit = await structure_lookup.resolve_structure(
        gene="SRP1", taxid=559292, max_aa_pos=428
    )

    assert hit is not None
    assert hit.accession == "Q02821"
    assert hit.pdb_ids == ["1BK5", "1EE4"]


async def test_returns_none_when_every_candidate_is_too_short(fake_uniprot):
    """A resolution failure, not an invitation to guess.

    Falling back to the first entry here is the same bug the SRP1 case
    describes, reached by a different path.
    """
    fake_uniprot.payload = _payload(
        _entry("P10863", 254),
        _entry("Q99999", 300, ["1ABC"]),
    )

    assert await structure_lookup.resolve_structure(
        gene="SRP1", taxid=559292, max_aa_pos=428
    ) is None


async def test_first_candidate_wins_when_it_fits(fake_uniprot):
    """The ordinary case: UniProt's own ranking is respected when nothing
    contradicts it."""
    fake_uniprot.payload = _payload(
        _entry("P00330", 348, ["4W6Z"]),
        _entry("P00331", 348, ["1ABC"]),
    )

    hit = await structure_lookup.resolve_structure(
        gene="ADH1", taxid=559292, max_aa_pos=100
    )
    assert hit.accession == "P00330"


# --- Resolved vs. no structure -----------------------------------------------


async def test_resolved_without_pdbs_is_not_a_failure(fake_uniprot):
    """65% of resolved genes have no structure. That is a normal outcome and
    must stay distinguishable from "could not resolve"."""
    fake_uniprot.payload = _payload(_entry("P12685", 1235))

    hit = await structure_lookup.resolve_structure(
        gene="TRK1", taxid=559292, max_aa_pos=400
    )
    assert hit is not None
    assert hit.accession == "P12685"
    assert hit.pdb_ids == []


async def test_no_results_returns_none(fake_uniprot):
    fake_uniprot.payload = _payload()
    assert await structure_lookup.resolve_structure(
        gene="NOSUCHGENE", taxid=559292, max_aa_pos=10
    ) is None


# --- Guards on what is queried at all ----------------------------------------


async def test_absent_taxid_issues_no_request(fake_uniprot):
    """An unscoped gene lookup is a wrong answer, not a degraded one.

    Cross-organism symbol collision is far worse than the within-organism SRP1
    case, so a missing taxid must stop the query rather than widen it.
    """
    assert await structure_lookup.resolve_structure(
        gene="ADH1", taxid=None, max_aa_pos=100
    ) is None
    assert fake_uniprot.calls == []


async def test_hyphenated_gene_survives_the_query(fake_uniprot):
    """Entrez parsed the hyphen in YDR524W-C as a boolean and returned 4,342
    unrelated structures. This pins that the UniProt path does not."""
    fake_uniprot.payload = _payload(_entry("P0C1Z1", 102))

    await structure_lookup.resolve_structure(
        gene="YDR524W-C", taxid=559292, max_aa_pos=50
    )

    assert "YDR524W-C" in fake_uniprot.calls[0]


async def test_query_is_organism_scoped_and_reviewed(fake_uniprot):
    fake_uniprot.payload = _payload()
    await structure_lookup.resolve_structure(
        gene="ADH1", taxid=559292, max_aa_pos=10
    )

    url = fake_uniprot.calls[0]
    assert "gene_exact" in url
    assert "559292" in url
    assert "reviewed" in url


# --- Failure style -----------------------------------------------------------


async def test_transport_failure_returns_none(monkeypatch):
    """The viewer is additive. A UniProt outage means no structure button
    works, never a 500 on the variants table."""
    def _boom(url, *, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(structure_lookup, "_get", _boom)

    assert await structure_lookup.resolve_structure(
        gene="ADH1", taxid=559292, max_aa_pos=10
    ) is None


async def test_malformed_response_returns_none(fake_uniprot):
    fake_uniprot.payload = b"<html>gateway timeout</html>"
    assert await structure_lookup.resolve_structure(
        gene="ADH1", taxid=559292, max_aa_pos=10
    ) is None


async def test_entry_without_a_length_is_skipped(fake_uniprot):
    """No length means the guard cannot run, and an unguarded candidate is
    exactly what this module exists to refuse."""
    fake_uniprot.payload = _payload(
        {"primaryAccession": "P00000", "uniProtKBCrossReferences": []},
        _entry("Q02821", 542, ["1BK5"]),
    )

    hit = await structure_lookup.resolve_structure(
        gene="SRP1", taxid=559292, max_aa_pos=428
    )
    assert hit.accession == "Q02821"


async def test_non_pdb_cross_references_are_ignored(fake_uniprot):
    """The cross-reference list holds dozens of databases; only PDB is a
    structure."""
    fake_uniprot.payload = _payload(
        {
            "primaryAccession": "P00330",
            "sequence": {"length": 348},
            "uniProtKBCrossReferences": [
                {"database": "STRING", "id": "not-a-structure"},
                {"database": "PDB", "id": "4W6Z"},
                {"database": "AlphaFoldDB", "id": "P00330"},
            ],
        }
    )

    hit = await structure_lookup.resolve_structure(
        gene="ADH1", taxid=559292, max_aa_pos=100
    )
    assert hit.pdb_ids == ["4W6Z"]


# --- Caching -----------------------------------------------------------------


async def test_a_repeated_lookup_does_not_requery(fake_uniprot):
    fake_uniprot.payload = _payload(_entry("P00330", 348, ["4W6Z"]))

    first = await structure_lookup.resolve_structure(
        gene="ADH1", taxid=559292, max_aa_pos=100
    )
    second = await structure_lookup.resolve_structure(
        gene="ADH1", taxid=559292, max_aa_pos=100
    )

    assert first.accession == second.accession
    assert len(fake_uniprot.calls) == 1


async def test_a_miss_is_cached_too(fake_uniprot):
    """65% of lookups miss. An uncached miss means every page render
    re-queries every absent gene."""
    fake_uniprot.payload = _payload()

    await structure_lookup.resolve_structure(gene="NOPE", taxid=559292, max_aa_pos=10)
    await structure_lookup.resolve_structure(gene="NOPE", taxid=559292, max_aa_pos=10)

    assert len(fake_uniprot.calls) == 1


async def test_a_cached_answer_is_not_reused_for_a_residue_it_cannot_hold(
    fake_uniprot,
):
    """The SRP1 case, reached through the cache instead of the ranking.

    Found by running the resolver against live UniProt, not by these tests:
    every other test here starts from a clean cache, so none of them could see
    it. Asking for SRP1 at residue 100 first legitimately resolves TIR1
    (254aa) -- it is UniProt's top hit and it does contain residue 100. Serving
    that stored answer to a later residue-428 variant is the exact bug the
    length guard exists to prevent, arrived at from a different direction.
    """
    fake_uniprot.payload = _payload(
        _entry("P10863", 254),
        _entry("Q02821", 542, ["1BK5"]),
    )

    low = await structure_lookup.resolve_structure(
        gene="SRP1", taxid=559292, max_aa_pos=100
    )
    assert low.accession == "P10863"

    high = await structure_lookup.resolve_structure(
        gene="SRP1", taxid=559292, max_aa_pos=428
    )
    assert high.accession == "Q02821"


async def test_a_cached_miss_is_not_reused_for_a_smaller_residue(fake_uniprot):
    """A miss records the residue that failed, not a protein length.

    A gene that could not satisfy residue 900 may still satisfy residue 10, so
    reusing the miss downward would manufacture a false negative.
    """
    fake_uniprot.payload = _payload(_entry("P00330", 348, ["4W6Z"]))

    assert await structure_lookup.resolve_structure(
        gene="ADH1", taxid=559292, max_aa_pos=900
    ) is None

    hit = await structure_lookup.resolve_structure(
        gene="ADH1", taxid=559292, max_aa_pos=10
    )
    assert hit is not None and hit.accession == "P00330"


async def test_the_cache_is_scoped_by_organism(fake_uniprot):
    """The same symbol in two organisms is two different proteins -- the
    collision this design is most concerned with."""
    fake_uniprot.payload = _payload(_entry("P00330", 348, ["4W6Z"]))
    await structure_lookup.resolve_structure(gene="ADH1", taxid=559292, max_aa_pos=100)
    await structure_lookup.resolve_structure(gene="ADH1", taxid=185431, max_aa_pos=100)

    assert len(fake_uniprot.calls) == 2
