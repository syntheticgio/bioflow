"""Resolving a protein accession to its deposited structures.

The sibling of `structure_lookup.py`, which answers the same question from a
gene symbol. The difference is not cosmetic: that module's central problem is
that a symbol is ambiguous, and most of its code is the length guard that
disambiguates. An accession is unambiguous, so none of that applies -- which
is why this is a separate module and a separate cache rather than a parameter
on that one.

What replaces it is a *selection* problem specific to RefSeq. Measured against
the live API on 2026-08-17: `xref:refseq-NP_000537` (human TP53) returns three
UniProt entries -- P04637 with 295 PDB structures, alongside isoform entries
with none. Taking the first result would show "no structure available" for one
of the best-characterised proteins in the PDB.

A known limitation, recorded because nothing here can catch it: a
version-stripped RefSeq ID can cross-reference to an unexpected entry.
Measured, `xref:refseq-NP_009342` returns a 212aa entry where yeast CDC19 is
roughly 500aa. The gene-symbol path would catch that with its length guard;
here there is no residue position to check against. The mitigation is that the
resolved accession and protein name are returned and displayed, so a wrong
answer is visible to the reader rather than silent.

Uses stdlib urllib rather than httpx, matching `uniprot.py` and
`structure_lookup.py`; httpx is a dev-only dependency here.
"""

import asyncio
import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from app.logging import get_logger
from app.metadata.protein_headers import ProteinRef, RefKind
from app.models import ProteinSequenceLookup, ProteinStructureLookup

log = get_logger(__name__)

_SEARCH_URL = "https://rest.uniprot.org/uniprotkb/search"

# Matches structure_lookup's budget, for the same reason: long enough for a
# cold UniProt response, short enough that a click does not appear to hang.
_TIMEOUT_SECONDS = 20.0

# A RefSeq cross-reference returns a handful of entries at most (TP53, the
# worst case measured, returns three). Enough to apply the selection rule
# without turning one lookup into a scan.
_MAX_CANDIDATES = 10


class ProteinStructureUnavailable(Exception):
    """UniProt could not be queried at all: timeout, refused connection, or an
    unreadable response.

    Raised rather than returned as None, because None already means the
    opposite situation -- a query that succeeded and matched nothing. That one
    is a permanent answer and gets cached as a negative row; this one is
    transient, must not be cached, and is the only outcome the UI offers a
    retry for.
    """


@dataclass(frozen=True)
class StructureHit:
    """One accession resolved, with whatever structures it has.

    An empty `pdb_ids` is a successful resolution, not a failure. The UI says
    "no experimental structure has been deposited" for that, and "this
    accession matched nothing in UniProt" for a None return -- two different
    sentences that must not be collapsed into one.
    """

    accession: str
    protein_name: str | None
    pdb_ids: list[str]


def _get(url: str, *, timeout: float = _TIMEOUT_SECONDS) -> bytes:
    """The transport, isolated so tests can replace it without a network."""
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read()


def _query_for(ref: ProteinRef) -> str:
    if ref.kind is RefKind.REFSEQ:
        # UniProt's cross-reference index, keyed on the unversioned accession.
        return f"xref:refseq-{ref.accession}"
    return f"accession:{ref.accession}"


def _pdb_ids(entry: dict) -> list[str]:
    """PDB cross-references only.

    The list holds dozens of databases. AlphaFoldDB in particular is
    deliberately excluded: a predicted model and a solved one warrant different
    confidence, and prediction is the follow-up this design defers rather than
    something to smuggle in behind the same button.
    """
    refs = entry.get("uniProtKBCrossReferences")
    if not isinstance(refs, list):
        return []
    return [
        ref["id"]
        for ref in refs
        if isinstance(ref, dict)
        and ref.get("database") == "PDB"
        and isinstance(ref.get("id"), str)
    ]


def _is_reviewed(entry: dict) -> bool:
    entry_type = (entry.get("entryType") or "").lower()
    return "reviewed" in entry_type and "unreviewed" not in entry_type


def _protein_name(entry: dict) -> str | None:
    description = entry.get("proteinDescription")
    if not isinstance(description, dict):
        return None
    recommended = description.get("recommendedName")
    if not isinstance(recommended, dict):
        return None
    full = recommended.get("fullName")
    if not isinstance(full, dict):
        return None
    value = full.get("value")
    return value if isinstance(value, str) else None


def _choose(entries: list) -> StructureHit | None:
    """The best candidate: reviewed first, then structure-bearing.

    Both halves are load-bearing and both come from measurement. Reviewed-first
    keeps an isoform entry from beating the curated one; structure-bearing
    among equals keeps a reviewed entry with no PDB references from hiding a
    reviewed one with 295 of them.
    """
    candidates = [e for e in entries if isinstance(e, dict) and e.get("primaryAccession")]
    if not candidates:
        return None

    best = max(
        candidates,
        key=lambda e: (_is_reviewed(e), bool(_pdb_ids(e))),
    )
    return StructureHit(
        accession=best["primaryAccession"],
        protein_name=_protein_name(best),
        pdb_ids=_pdb_ids(best),
    )


async def resolve(ref: ProteinRef) -> StructureHit | None:
    """The structures for one accession, or None if it resolved to nothing.

    None means the query succeeded and matched nothing -- an unresolvable
    accession, a permanent answer that is cached as a negative row. The same
    None covers a query UniProt rejected outright (HTTP 400, e.g. an invalid
    accession format): also permanent for this accession, cached the same way.
    A UniProt outage is a different situation with a different remedy (retry),
    so it raises ProteinStructureUnavailable rather than returning None.
    """
    cached = await ProteinStructureLookup.find_one(
        ProteinStructureLookup.accession == ref.accession
    )
    if cached is not None:
        if cached.resolved_accession is None:
            return None
        return StructureHit(
            accession=cached.resolved_accession,
            protein_name=cached.protein_name,
            pdb_ids=list(cached.pdb_ids),
        )

    query = urllib.parse.urlencode(
        {
            "query": _query_for(ref),
            "fields": "accession,xref_pdb,protein_name,reviewed",
            "format": "json",
            "size": str(_MAX_CANDIDATES),
        }
    )

    try:
        # A blocking socket on the event loop would stall every other request.
        raw = await asyncio.to_thread(
            _get, f"{_SEARCH_URL}?{query}", timeout=_TIMEOUT_SECONDS
        )
        results = json.loads(raw).get("results", [])
    except urllib.error.HTTPError as exc:
        if exc.code != 400:
            raise ProteinStructureUnavailable(str(exc)) from exc
        # A 400 is UniProt rejecting the query itself -- an invalid accession
        # format, for instance. That is a permanent answer for this accession,
        # the same shape as an empty result list, so it flows into the same
        # _choose/_remember path and is cached as a negative. Any other 4xx
        # (429 rate-limit in particular) and every 5xx is a real outage and
        # raises like everything below.
        log.info(
            "protein_structure_query_rejected",
            accession=ref.accession,
            error=str(exc),
        )
        results = []
    except Exception as exc:
        # Deliberately not cached. A cached failure is indistinguishable from a
        # cached "no structure", and this collection has no expiry -- so an
        # outage would become permanent until someone dropped the collection.
        log.info(
            "protein_structure_lookup_failed", accession=ref.accession, error=str(exc)
        )
        raise ProteinStructureUnavailable(str(exc)) from exc

    hit = _choose(results if isinstance(results, list) else [])
    await _remember(ref.accession, hit)
    return hit


async def resolve_by_sequence(sequence: str) -> StructureHit | None:
    """Resolve a protein sequence to structures via UniProt sequence search.

    The fallback for records whose headers name no accession (issue #534): read
    the record's sequence from its byte offset and query UniProt for an exact
    sequence match, reusing `_choose()` for candidate selection.

    Returns ``StructureHit`` on a match (with possibly empty ``pdb_ids``),
    ``None`` on a UniProt outage. A no-match result is cached as a negative, so
    the caller can distinguish "searched, nothing found" (cached miss) from
    "could not reach UniProt" (uncached None) by checking the collection.
    """
    sequence_hash = hashlib.sha256(sequence.encode("utf-8")).hexdigest()

    cached = await ProteinSequenceLookup.find_one(
        ProteinSequenceLookup.sequence_hash == sequence_hash
    )
    if cached is not None:
        if cached.resolved_accession is None:
            return None
        return StructureHit(
            accession=cached.resolved_accession,
            protein_name=cached.protein_name,
            pdb_ids=list(cached.pdb_ids),
        )

    # UniProt's `sequence:` field finds entries whose sequence is identical to
    # the query. The full uniline string is passed as-is; UniProt accepts raw
    # amino-acid sequences. Same fields and selection as accession resolution.
    query = urllib.parse.urlencode(
        {
            "query": f'sequence:"{sequence}"',
            "fields": "accession,xref_pdb,protein_name,reviewed",
            "format": "json",
            "size": str(_MAX_CANDIDATES),
        }
    )

    try:
        raw = await asyncio.to_thread(
            _get, f"{_SEARCH_URL}?{query}", timeout=_TIMEOUT_SECONDS
        )
        results = json.loads(raw).get("results", [])
    except Exception as exc:
        # Deliberately not cached: a cached failure is indistinguishable from a
        # cached miss, and this collection has no expiry. An outage must not
        # become permanent.
        log.info(
            "protein_structure_sequence_lookup_failed",
            sequence_hash=sequence_hash,
            error=str(exc),
        )
        return None

    hit = _choose(results if isinstance(results, list) else [])
    await _remember_sequence(sequence_hash, hit)
    return hit


async def _remember_sequence(
    sequence_hash: str, hit: StructureHit | None
) -> None:
    """Store a sequence-resolution result, including a negative one.

    Mirrors `_remember` but keyed by sequence hash. Upsert handles concurrent
    lookups of the same sequence reaching here together.
    """
    values = {
        "resolved_accession": hit.accession if hit else None,
        "protein_name": hit.protein_name if hit else None,
        "pdb_ids": hit.pdb_ids if hit else [],
    }
    await ProteinSequenceLookup.find_one(
        ProteinSequenceLookup.sequence_hash == sequence_hash
    ).upsert(
        {"$set": values},
        on_insert=ProteinSequenceLookup(sequence_hash=sequence_hash, **values),
    )


async def _remember(accession: str, hit: StructureHit | None) -> None:
    """Store a result, including a negative one.

    Upsert rather than insert: two views of the same accession can reach here
    concurrently, and the unique index would turn the loser into an error over
    an answer that is already correct.
    """
    values = {
        "resolved_accession": hit.accession if hit else None,
        "protein_name": hit.protein_name if hit else None,
        "pdb_ids": hit.pdb_ids if hit else [],
    }
    await ProteinStructureLookup.find_one(
        ProteinStructureLookup.accession == accession
    ).upsert(
        {"$set": values},
        on_insert=ProteinStructureLookup(accession=accession, **values),
    )
